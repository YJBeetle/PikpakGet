"""PikPak drive API client.

Only the standard library is used. Authentication is a normal password sign-in
with the same public application client that the official mobile app and several
open-source SDKs use, so the resulting session is refreshable without a browser,
captcha GUI, or any interactive step.

Endpoints and request shapes match what the official desktop client sends; each
mutating call carries a short-lived shield `captcha_token` minted locally.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

USER_API = 'https://user.mypikpak.com'
DRIVE_API = 'https://api-drive.mypikpak.com/drive'
VIP_API = 'https://api-drive.mypikpak.com/vip'

# Public application client constants (not account credentials).
CLIENT_ID = 'YNxT9w7GMdWvEOKa'
CLIENT_SECRET = 'dbw2OtmVEeuUvIptb1Coyg'
CLIENT_VERSION = '1.47.1'
PACKAGE_NAME = 'com.pikcloud.pikpak'
SDK_VERSION = '2.0.4.204000'

CAPTCHA_SALTS = (
    'Gez0T9ijiI9WCeTsKSg3SMlx',
    'zQdbalsolyb1R/',
    'ftOjr52zt51JD68C3s',
    'yeOBMH0JkbQdEFNNwQ0RI9T3wU/v',
    'BRJrQZiTQ65WtMvwO',
    'je8fqxKPdQVJiy1DM6Bc9Nb1',
    'niV',
    '9hFCW2R1',
    'sHKHpe2i96',
    'p7c5E6AcXQ/IJUuAEC9W6',
    '',
    'aRv9hjc9P+Pbn+u3krN6',
    'BzStcgE8qVdqjEH16l4',
    'SqgeZvL5j9zoHP95xWHt',
    'zVof5yaJkPe3VFpadPof',
)

TOKEN_REFRESH_MARGIN = 900          # refresh when <15 min left
CAPTCHA_TTL = 240                   # the server issues 300s
THROTTLE_BACKOFF = (30, 120, 480, 900)
# A dropped TLS session is routine on this network, not a verdict about the link:
# retry it quickly, and only give up after several attempts.
NETWORK_RETRY_WAITS = (5, 15, 45, 120, 240)
SHARE_ID = re.compile(
    r'(?:mypikpak\.com|pickpackapp\.com|mypikpak\.net|pikpak\.me)/s/([A-Za-z0-9_\-]{8,})')
# The free tier's daily downstream traffic cap (20 GB) arrives as an ordinary HTTP 400
# carrying an upsell body, so nothing in the retry ladder would recognise it: every
# remaining file would spend its attempts against the same wall.
TRAFFIC_CAP = re.compile(r'downstream traffic|has exceeded the limit', re.I)


class PikPakError(RuntimeError):
    """An API failure, with enough detail for the caller to tell a bad link apart
    from a rate limit and from a dead session."""

    def __init__(self, message, status=None, code=None, action=None, throttled=False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.action = action
        self.throttled = throttled

    def __str__(self):
        return f'{super().__str__()} [HTTP {self.status} code={self.code} action={self.action}]'


def _safe_body(body):
    """The server's own answer, minus anything that could be replayed.

    A sign-in refusal is otherwise undiagnosable — `AccessProhibited` alone does not
    say whether the account, the device fingerprint or the egress address was refused —
    and the fields that would say so sit next to a live captcha token, which is exactly
    what a user would paste into a bug report."""
    kept = {}
    for key, value in (body or {}).items():
        if 'token' in key.lower() or key.lower() in ('captcha_vid', 'nonce', 'captcha_sign'):
            kept[key] = '…省略…'
        elif isinstance(value, str) and len(value) > 200:
            kept[key] = value[:200] + '…'
        else:
            kept[key] = value
    return json.dumps(kept, ensure_ascii=False)


class TrafficCapped(PikPakError):
    """Today's downstream traffic quota is used up. Backing off for minutes fixes
    nothing — the counter is daily — so the run ends and the same command is re-run
    after it rolls over."""


def captcha_sign(client_id, device_id, timestamp=None):
    """The shield signature: an md5 chain over the salt list, fed with
    clientId + clientVersion + packageName + deviceId + timestamp. The server
    recomputes it, so every input must match what is sent alongside it."""
    import hashlib
    stamp = str(timestamp or int(time.time() * 1000))
    state = f'{client_id}{CLIENT_VERSION}{PACKAGE_NAME}{device_id}{stamp}'
    for salt in CAPTCHA_SALTS:
        state = hashlib.md5((state + salt).encode()).hexdigest()
    return '1.' + state, stamp


def parse_share_url(text):
    """`https://mypikpak.com/s/<share_id>[?password=..]` -> (share_id, pass_code)."""
    match = SHARE_ID.search(text or '')
    if not match:
        raise ValueError(f'not a PikPak share link: {text[:60]!r}')
    params = urllib.parse.urlparse(text).query
    code = (urllib.parse.parse_qs(params).get('password')
            or urllib.parse.parse_qs(params).get('pass_code') or [''])[0]
    return match.group(1), code


def _loads(raw):
    try:
        return json.loads(raw.decode('utf-8'))
    except Exception:                                          # noqa: BLE001
        return {}


def _is_folder(item):
    return item.get('kind') == 'drive#folder'


class Session:
    """A refreshable credential kept in a 0600 file. The refresh token is single
    use and rotates, so it is rewritten on every refresh."""

    def __init__(self, path):
        self.path = path
        self.data = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding='utf-8') as handle:
                    self.data = json.load(handle)
            except (ValueError, OSError):
                self.data = {}

    @property
    def access_token(self):
        return self.data.get('access_token')

    @property
    def user_id(self):
        return self.data.get('sub') or ''

    def expires_in(self, now=None):
        return float(self.data.get('expires_at') or 0) - (now or time.time())

    def valid(self, margin=0):
        return bool(self.access_token) and self.expires_in() > margin

    def store(self, payload):
        payload = dict(payload)
        payload.setdefault('source', 'password-login')
        self.data = payload
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        tmp = f'{self.path}.tmp'
        # create it private: opening with the default umask first would leave a
        # readable window, and chmod after os.replace is even later
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def forget(self):
        self.data = {}
        if self.path and os.path.exists(self.path):
            os.remove(self.path)


def load_device_id(path):
    """A stable per-installation id, persisted so the shield does not see a new
    device on every run."""
    if os.path.exists(path):
        with open(path, encoding='utf-8') as handle:
            stored = handle.read().strip()
        if stored:
            return stored
    fresh = uuid.uuid4().hex
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(fresh)
    return fresh


class Client:
    def __init__(self, session_path=None, device_id_path=None, logger=None, timeout=45):
        self.log = logger or (lambda message, level='info': None)
        self.timeout = timeout
        self.session = Session(session_path)
        self.device_id = load_device_id(device_id_path) if device_id_path else uuid.uuid4().hex
        self._captchas = {}
        self._requests = 0
        # only a session that provably cannot be refreshed justifies stopping a run;
        # see refresh()
        self.session_dead = False

    # ------------------------------------------------------------------ session
    def sign_in(self, username, password):
        """Password login. The captcha challenge for an anonymous sign-in carries
        no signature, because the server has no session to check one against."""
        signin_url = f'{USER_API}/v1/auth/signin'
        metas = ({'email': username} if '@' in username
                 else {'phone_number': username} if username.isdigit()
                 else {'username': username})
        status, body = self._raw('POST', f'{USER_API}/v1/shield/captcha/init', auth=False,
                                 json_body={'client_id': CLIENT_ID, 'action': f'POST:{signin_url}',
                                            'device_id': self.device_id, 'meta': metas},
                                 headers=self._client_headers())
        token = body.get('captcha_token') if status == 200 else None
        if not token:
            if body.get('url'):
                raise PikPakError('登录需要先通过人工验证码（浏览器完成一次后再试）',
                                  status=status, action='signin')
            raise PikPakError(f'无法取得登录验证码: {_safe_body(body)}',
                              status=status, action='signin')
        status, body = self._raw('POST', signin_url, auth=False, form={
            'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET, 'username': username,
            'password': password, 'captcha_token': token},
            headers={**self._client_headers(), 'Content-Type': 'application/x-www-form-urlencoded'})
        if status != 200 or not body.get('access_token'):
            detail = str(body.get('error_description') or body)
            hint = ''
            if 'verification' in detail.lower():
                hint = '（若反复出现 verification failed：连错会要求人工校验，请等几分钟再试或先登录一次官方客户端）'
            elif 'prohibit' in detail.lower():
                # seen from two causes, neither of them the password: a refused egress
                # address (fixed by routing that machine out another way) and, less
                # often, a device fingerprint PikPak has never met. Retrying fixes
                # neither and is how a soft refusal becomes real risk control.
                hint = ('（服务端拒绝的是"登录"这个动作本身，不代表密码错。先试换一个出口地址：'
                        f'API 与分段下载都读 *_proxy 环境变量；或把已登录机器上 '
                        f'{os.path.join(os.path.dirname(self.session.path), "device_id")} '
                        '这个设备号复制过来。别连续重试，那可能升级成真风控。）')
            raise PikPakError(f'登录失败: {detail}{hint}\n服务端原文: {_safe_body(body)}',
                              status=status, action='signin')
        self.session.store(self._token_record(body))
        # the path belongs in the message: `--login` without `--state-dir` writes to
        # the default directory, and a later run that does pass one finds no session
        # there and reads as "the login dropped"
        self.log(f'登录成功，会话已保存到 {self.session.path}（权限 600）')
        return self.session.data

    @staticmethod
    def _token_record(body):
        return {'access_token': body['access_token'], 'refresh_token': body.get('refresh_token'),
                'token_type': body.get('token_type', 'Bearer'), 'sub': body.get('sub'),
                'expires_at': time.time() + float(body.get('expires_in') or 7200)}

    def refresh(self):
        if not self.session.data.get('refresh_token'):
            self.session_dead = True
            raise PikPakError('会话没有 refresh_token，请重新登录', action='refresh')
        status, body = self._raw('POST', f'{USER_API}/v1/auth/token', auth=False, json_body={
            'client_id': CLIENT_ID, 'grant_type': 'refresh_token',
            'refresh_token': self.session.data['refresh_token']}, headers=self._client_headers())
        if status != 200 or not body.get('access_token'):
            # only a refusal *of this credential* proves the session is dead: a 5xx is
            # the auth endpoint being unhappy, and ending a multi-day run over that
            # would be worse than retrying it. A socket that never got there proves
            # nothing at all.
            self.session_dead = status is not None and 400 <= status < 500
            raise PikPakError(f'会话续期失败: {body.get("error_description") or body}（请重新登录）',
                              status=status, action='refresh')
        self.session.store(self._token_record(body))
        self.session_dead = False
        self.log('会话已自动续期')

    def ensure_session(self, force=False):
        if not self.session.access_token:
            raise PikPakError(f'还没有登录（会话文件 {self.session.path} 不存在或没有 '
                             f'access_token）：先运行 `--login <邮箱>`，注意它的 '
                             f'--state-dir 要和这里一致', action='session')
        if force or not self.session.valid(TOKEN_REFRESH_MARGIN):
            self.refresh()
        return self.session.access_token

    # ----------------------------------------------------------------- plumbing
    def _client_headers(self):
        return {'x-client-id': CLIENT_ID, 'x-device-id': self.device_id,
                'x-client-version': CLIENT_VERSION, 'x-sdk-version': SDK_VERSION,
                'x-protocol-version': '301', 'User-Agent': f'{PACKAGE_NAME}/{CLIENT_VERSION}'}

    def _raw(self, method, url, *, params=None, json_body=None, form=None, auth=True, headers=None):
        if params:
            url = f'{url}?{urllib.parse.urlencode(params, doseq=True)}'
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
        elif json_body is not None:
            data = json.dumps(json_body).encode()
        else:
            data = None
        base = {**self._client_headers(), 'Content-Type': 'application/json',
                **({'Authorization': f'Bearer {self.session.access_token}'} if auth
                   and self.session.access_token else {})}
        merged = {key: value for key, value in {**base, **(headers or {})}.items()
                  if value is not None}
        self._requests += 1
        try:
            request = urllib.request.Request(url, data=data, method=method, headers=merged)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, _loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _loads(error.read())
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return None, {'error_description': f'{type(error).__name__}: {error}'}

    @staticmethod
    def _throttled(status, body):
        text = str(body.get('error_description') or body.get('error') or '').lower()
        return (status in (429, 503) or body.get('error_code') in (4002, 4003)
                or 'slow down' in text or 'too many' in text or 'rate' in text)

    def captcha_token(self, action):
        cached = self._captchas.get(action)
        if cached and cached[1] > time.time():
            return cached[0]
        sign, stamp = captcha_sign(CLIENT_ID, self.device_id)
        status, body = self._raw('POST', f'{USER_API}/v1/shield/captcha/init',
                                 json_body={'client_id': CLIENT_ID, 'action': action,
                                            'device_id': self.device_id,
                                            'meta': {'captcha_sign': sign, 'timestamp': stamp,
                                                     'client_version': CLIENT_VERSION,
                                                     'package_name': PACKAGE_NAME,
                                                     'user_id': self.session.user_id}},
                                 headers={'Authorization': f'Bearer {self.ensure_session()}'})
        token = body.get('captcha_token') if status == 200 else None
        if not token:
            raise PikPakError(f'验证码 token 申请失败: {body.get("error_description") or body}',
                              status=status, code=body.get('error_code'), action=action,
                              throttled=self._throttled(status, body))
        self._captchas[action] = (token, time.time() + CAPTCHA_TTL)
        return token

    def call(self, method, path, *, params=None, json_body=None, base=DRIVE_API,
             action=None, retries=None):
        """One API call. The shield token is per action, so it is minted lazily
        and reused inside its lifetime."""
        action = action or f'{method}:/drive{path}'
        attempts = retries if retries is not None else len(NETWORK_RETRY_WAITS)
        last = {}
        for attempt in range(attempts):
            status, body = self._raw(method, f'{base}{path}', params=params, json_body=json_body,
                                     headers={'x-action': action,
                                              'x-captcha-token': self.captcha_token(action)})
            if status and 200 <= status < 300:
                return body
            last = {'status': status, **body}
            problem = str(body.get('error_description') or body.get('error') or '请求失败')
            if status is None:                          # socket/TLS/DNS level failure
                wait = NETWORK_RETRY_WAITS[min(attempt, len(NETWORK_RETRY_WAITS) - 1)]
                self.log(f'网络抖动（{problem[:70]}），{wait}s 后重试', 'warn')
                time.sleep(wait)
                continue
            if TRAFFIC_CAP.search(problem) or TRAFFIC_CAP.search(str(body)):
                # a fact about the account today, not about this file: no back-off
                # ladder and no retries, the run simply ends and is re-run later
                raise TrafficCapped(f'今日下行流量已到上限: {problem[:160]}', status=status,
                                    code=body.get('error_code'), action=action)
            throttled = self._throttled(status, body)
            if status in (401, 403) and 'token' in problem.lower():
                self._captchas.clear()
                self.ensure_session(force=True)
                continue
            if throttled:
                wait = THROTTLE_BACKOFF[min(attempt, len(THROTTLE_BACKOFF) - 1)]
                self.log(f'被限流（HTTP {status}），等待 {wait}s 后重试', 'warn')
                self._captchas.clear()
                time.sleep(wait)
                continue
            raise PikPakError(problem, status=status, code=body.get('error_code'),
                              action=action, throttled=throttled)
        raise PikPakError(str(last.get('error_description') or '重试后仍失败'),
                          status=last.get('status'), action=action, throttled=True)

    # --------------------------------------------------------------------- read
    def about(self):
        return self.call('GET', '/v1/about',
                         params={'with_quotas': 'true', 'with_third_party_information': 'false'})

    def space(self):
        """Cloud bytes. Trash counts against `limit`, so only a permanent delete
        reclaims room."""
        quota = self.about().get('quota') or {}
        limit = int(quota.get('limit') or 0)
        usage = int(quota.get('usage') or 0)
        return {'limit': limit, 'usage': usage,
                'in_trash': int(quota.get('usage_in_trash') or 0),
                'free': max(limit - usage, 0) if limit else 0}

    def offline_task_slots(self):
        quota = (self.about().get('quotas') or {}).get('cloud_download') or {}
        return {'limit': int(quota.get('limit') or 0), 'usage': int(quota.get('usage') or 0)}

    def list_folder(self, parent_id='*', trashed=False):
        return list(self._paginate('/v1/files', {
            'parent_id': parent_id, 'limit': 200, 'order': 3,
            'filters': json.dumps({'trashed': {'eq': bool(trashed)}}), 'with_audit': 'true'}))

    def list_trash(self):
        return list(self._paginate('/v1/files', {'parent_id': '*', 'limit': 200,
                                                 'trashed': 'true', 'order': 3}))

    def file(self, file_id):
        return self.call('GET', f'/v1/files/{file_id}')

    def download_url(self, file_id):
        """A short-lived signed URL. Fetch it right before each use; the CDN
        rejects it once it is stale, which looks like a download failure."""
        data = self.call('GET', f'/v1/files/{file_id}', params={'usage': 'FETCH'})
        links = data.get('links') or {}
        url = ((links.get('application/octet-stream') or {}).get('url')
               or (data.get('link') or {}).get('url') or data.get('web_content_link'))
        if not url:
            raise PikPakError(f'取不到下载地址（返回字段 {sorted(data)}）',
                              action='GET:/drive/v1/files/<id>')
        return url, {'name': data.get('name'), 'size': int(data.get('size') or 0),
                     'hash': data.get('hash'), 'mime': data.get('mime_type'),
                     'phase': data.get('phase')}

    # -------------------------------------------------------------------- share
    def share_info(self, share_id, pass_code=''):
        params = {'share_id': share_id, 'limit': 200, 'order': 3}
        if pass_code:
            params['pass_code'] = pass_code
        return self.call('GET', '/v1/share', params=params)

    def share_children(self, share_id, pass_code_token, parent_id):
        return list(self._paginate('/v1/share/detail', {
            'share_id': share_id, 'pass_code_token': pass_code_token,
            'parent_id': parent_id, 'limit': 200, 'order': 6}))

    @staticmethod
    def node(item, prefix=''):
        params = item.get('params') or {}
        folder = _is_folder(item)
        name = item.get('name') or ''
        return {'id': item.get('id'), 'name': name, 'prefix': prefix,
                'path': f'{prefix}/{name}' if prefix else name, 'is_folder': folder,
                'size': int(item.get('size') or params.get('total_size') or 0),
                'child_count': int(params.get('total_count') or 0) if folder else None,
                'hash': item.get('hash') or None, 'mime': item.get('mime_type') or ''}

    def walk_share(self, share_id, pass_code='', max_nodes=20000):
        """Flatten a share into files and folders. Costs no cloud space, so it is
        safe to run before deciding what to download. Folders expose their
        aggregate `total_size`, which makes sizing a subtree cheap."""
        info = self.share_info(share_id, pass_code)
        token = info.get('pass_code_token') or ''
        nodes = [self.node(item) for item in (info.get('files') or [])]
        queue = [(node['id'], node['path']) for node in nodes if node['is_folder']]
        while queue and len(nodes) < max_nodes:
            folder_id, prefix = queue.pop(0)
            for item in self.share_children(share_id, token, folder_id):
                node = self.node(item, prefix)
                nodes.append(node)
                if node['is_folder']:
                    queue.append((node['id'], node['path']))
                if len(nodes) >= max_nodes:
                    break
        return {'share_id': share_id, 'title': info.get('title'), 'pass_code_token': token,
                'file_num': info.get('file_num'), 'nodes': nodes,
                'truncated': bool(queue)}

    # ------------------------------------------------------------------- change
    def restore(self, share_id, pass_code_token, file_ids, parent_id='*'):
        """Copy shared files into this drive. `file_ids` may be files or folders;
        a folder copies its whole subtree, so pick folder ids deliberately when
        the cloud quota is small."""
        return self.call('POST', '/v1/share/restore', json_body={
            'share_id': share_id, 'pass_code_token': pass_code_token,
            'file_ids': list(file_ids), 'parent_id': parent_id})

    def task(self, task_id):
        return self.call('GET', f'/v1/tasks/{task_id}')

    def batch_trash(self, ids):
        return self.call('POST', '/v1/files:batchTrash', json_body={'ids': list(ids)})

    def batch_delete(self, ids):
        return self.call('POST', '/v1/files:batchDelete', json_body={'ids': list(ids)})

    def empty_trash(self):
        """Frees the whole trash. Destructive and account-wide: callers must show
        what is in the trash first."""
        return self.call('PATCH', '/v1/files/trash:empty')

    def cleanup(self, ids):
        """Trash then permanently delete our own copies, which is the only way to
        hand quota back to the next file."""
        ids = [item for item in ids if item]
        if not ids:
            return {}
        self.batch_trash(ids)
        return self.batch_delete(ids)

    # ----------------------------------------------------------------  internal
    def _paginate(self, path, params):
        page_token = None
        while True:
            request = dict(params)
            if page_token:
                request['page_token'] = page_token
            body = self.call('GET', path, params=request)
            for item in body.get('files') or []:
                yield item
            page_token = body.get('next_page_token')
            if not page_token:
                return
