"""Device-local account registry and nonblocking per-account leases."""
import fcntl
import hashlib
import json
import os

from .api import Client, PikPakError, Session


class Accounts:
    def __init__(self, home):
        self.home = home
        self.root = os.path.join(home, 'accounts')
        self.index = os.path.join(home, 'accounts.json')
        try:
            with open(self.index, encoding='utf-8') as handle:
                self.items = json.load(handle)
        except FileNotFoundError:
            self.items = []
        except (ValueError, OSError) as error:
            raise PikPakError(f'账号列表无法读取：{self.index}（{error}）') from error
        if not isinstance(self.items, list) or any(
                not isinstance(item, dict) or not item.get('id') or not item.get('label')
                for item in self.items):
            raise PikPakError(f'账号列表格式错误：{self.index}')

    def directory(self, account_id):
        return os.path.join(self.root, account_id)

    def client(self, item, logger=None):
        return Client(session_path=os.path.join(self.directory(item['id']), 'session.json'),
                      device_id_path=os.path.join(self.home, 'device_id'), logger=logger)

    def login(self, username, password, logger=None):
        # The server's user id, not the spelling of the login, identifies an account.
        # Lock an already known label before sign-in: issuing a fresh session
        # while a download uses its old one can invalidate that active session.
        known = [item for item in self.items if item['label'] == username]
        if len(known) > 1:
            raise PikPakError(f'账号名称 {username!r} 重复，请先用 --accounts 检查')
        known_lock = self.acquire(known[0]) if known else None
        if known and known_lock is None:
            raise PikPakError(f'账号 {username} 正在下载，稍后再登录')
        account_lock = None
        try:
            # Keep the new session in memory until the server identity is known.
            client = Client(device_id_path=os.path.join(self.home, 'device_id'), logger=logger)
            payload = client.sign_in(username, password)
            user_id = payload.get('sub')
            if not user_id:
                raise PikPakError('登录响应没有用户 ID，无法安全保存多账号会话')
            account_id = hashlib.sha256(str(user_id).encode()).hexdigest()[:24]
            directory = self.directory(account_id)
            item = {'id': account_id, 'label': username}
            if known and known[0]['id'] == account_id:
                account_lock, known_lock = known_lock, None
            else:
                account_lock = self.acquire(item)
                if account_lock is None:
                    raise PikPakError(f'账号 {username} 正在下载，稍后再登录')
            os.makedirs(directory, mode=0o700, exist_ok=True)
            Session(os.path.join(directory, 'session.json')).store(payload)
            os.makedirs(self.home, exist_ok=True)
            registry_lock = open(os.path.join(self.home, 'accounts.lock'), 'a+')
            try:
                fcntl.flock(registry_lock, fcntl.LOCK_EX)
                fresh = Accounts(self.home).items
                for index, existing in enumerate(fresh):
                    if existing['id'] == account_id:
                        fresh[index] = item
                        break
                else:
                    fresh.append(item)
                temporary = self.index + '.tmp'
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                    json.dump(fresh, handle, ensure_ascii=False, indent=1)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.index)
                self.items = fresh
            finally:
                registry_lock.close()
            return item
        finally:
            if account_lock:
                account_lock.close()
            if known_lock:
                known_lock.close()

    def find(self, label):
        matches = [item for item in self.items
                   if item['id'] == label or item['label'] == label]
        if len(matches) != 1:
            raise PikPakError(f'账号 {label!r} 未找到或名称重复；用 --accounts 查看账号 ID')
        return matches[0]

    def acquire(self, item):
        directory = self.directory(item['id'])
        os.makedirs(directory, mode=0o700, exist_ok=True)
        handle = open(os.path.join(directory, 'lock'), 'a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        return handle

    def choose(self, excluded=(), only=None):
        candidates = [self.find(only)] if only else self.items
        for item in candidates:
            if item['id'] in excluded:
                continue
            handle = self.acquire(item)
            if handle:
                return item, handle
        return None, None

    def logout(self, item):
        handle = self.acquire(item)
        if handle is None:
            raise PikPakError(f'账号 {item["label"]} 正在使用，不能退出登录')
        try:
            self.client(item).session.forget()
            registry_lock = open(os.path.join(self.home, 'accounts.lock'), 'a+')
            try:
                fcntl.flock(registry_lock, fcntl.LOCK_EX)
                fresh = [entry for entry in Accounts(self.home).items
                         if entry['id'] != item['id']]
                temporary = self.index + '.tmp'
                with open(temporary, 'w', encoding='utf-8') as output:
                    json.dump(fresh, output, ensure_ascii=False, indent=1)
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.index)
                self.items = fresh
            finally:
                registry_lock.close()
        finally:
            handle.close()
