"""Byte transfer: one resumable stream, or several concurrent ranged curl
segments spliced together afterwards.

The CDN throttles each connection (~0.5-0.7 MB/s on a free account) and appears to
cap the account too, so extra segments buy a sublinear gain at best. Both paths
survive interruption: the stream keeps a `.part` file, segments keep their own
partial bytes, and either resumes where it stopped.

Refusals and starvation are logged apart on purpose. They look the same from a
non-zero exit code, but the right reaction is the opposite: an HTTP refusal means
back off and lower concurrency, a starved lane means segmentation is pointless here
and a single stream will be faster.
"""
import glob as _glob
import os
import shutil
import subprocess
import time

from .api import PikPakError

CHUNK = 1 << 20
LAUNCH_STAGGER = 3                 # seconds between opening connections
STALL_LIMIT = 2048                 # bytes/s
STALL_SECONDS = 90                 # ... sustained, after which curl gives up
SEGMENT_RETRY_WAITS = (60, 300, 900)
PROBE_BYTES = 1


class SegmentRefused(PikPakError):
    """The CDN answered a ranged request with 4xx/5xx. That is a policy signal, not
    a slow lane, so the caller should drop to a single connection and back off."""


def _segment_path(target, connections):
    """Segment ranges depend on the connection count, so each width gets its own
    directory and a narrower plan can never read another plan's partial bytes."""
    return f'{target}.segs{connections}'


def plan_segments(total, connections, seg_dir):
    """Split `total` bytes into `connections` ranges and report how much of each
    is already on disk, so every segment resumes independently."""
    if connections < 1:
        raise ValueError('connections must be >= 1')
    os.makedirs(seg_dir, exist_ok=True)
    _check_segment_size(seg_dir, total)
    edges = [total * i // connections for i in range(connections)] + [total]
    plan = []
    for index in range(connections):
        start, end = edges[index], edges[index + 1] - 1
        if end < start:
            continue
        path = os.path.join(seg_dir, f'{index:03d}')
        want = end - start + 1
        have = os.path.getsize(path) if os.path.exists(path) else 0
        if have > want:                           # a stale segment is not usable
            os.remove(path)
            have = 0
        plan.append({'index': index, 'path': path, 'start': start, 'end': end,
                     'want': want, 'have': have})
    return plan


def _check_segment_size(seg_dir, total):
    """Wipe a segment directory that was built for a different file size.

    A resume assumes the bytes on disk are the prefix of the same content. If the
    sharer replaced the file behind the same id, that assumption is false and the
    old prefix would splice into the new file as corruption, so start clean."""
    marker = os.path.join(seg_dir, '.total')
    recorded = None
    if os.path.exists(marker):
        with open(marker, encoding='utf-8') as handle:
            recorded = handle.read().strip()
    if recorded is not None and recorded != str(total):
        for name in os.listdir(seg_dir):
            os.remove(os.path.join(seg_dir, name))
    # always record the current size, otherwise a wipe would repeat forever
    with open(marker, 'w', encoding='utf-8') as handle:
        handle.write(str(total))


def _segment_have(item):
    """Bytes that are *usable* for this segment. A curl that was still winding down
    when the next round started can append past the range, so anything beyond the
    segment's own length is ignored rather than trusted or thrown away."""
    if not os.path.exists(item['path']):
        return 0
    return min(os.path.getsize(item['path']), item['want'])


def segment_request(item):
    """Range and size limit for the next attempt at a segment, read from disk so a
    retry resumes at the true current length instead of the length planned earlier.
    `--max-filesize` is the guard: if the CDN ever ignores the Range header, curl
    aborts rather than letting us append a whole second copy onto the segment."""
    have = _segment_have(item)
    remaining = item['want'] - have
    return {'have': have, 'remaining': remaining,
            'range': f"{item['start'] + have}-{item['end']}",
            'max_filesize': remaining}


def build_command(item, url):
    request = segment_request(item)
    return ['curl', '-sS', '--fail', '--speed-limit', str(STALL_LIMIT),
            '--speed-time', str(STALL_SECONDS), '--retry', '2', '--retry-delay', '5',
            '--max-filesize', str(request['max_filesize']),
            '-r', request['range'], '-o', '-', '-A', 'Mozilla/5.0', url]


def overshot(plan):
    """Segments carrying trailing bytes past their range. Harmless (they are never
    read), but worth reporting because it means two writers hit the same file."""
    return [item['index'] for item in plan
            if os.path.exists(item['path']) and os.path.getsize(item['path']) > item['want']]


def reap(processes):
    """Terminate *and wait*: returning while a curl is still alive lets the next
    round open the same file in append mode alongside it, which is exactly how
    segments end up longer than their range."""
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _spawn(item, url):
    """Fetch one segment's remaining bytes in append mode."""
    if segment_request(item)['remaining'] <= 0:
        return None
    handle = open(item['path'], 'ab')
    try:
        return subprocess.Popen(build_command(item, url), stdout=handle,
                                stderr=subprocess.DEVNULL)
    finally:
        handle.close()


def probe_status(url, start=0, log=None, note=''):
    """Ask the CDN for one byte to learn *why* a segment failed. A 206 means the
    lane was simply starved; 4xx/5xx means it was refused."""
    command = ['curl', '-s', '-o', os.devnull, '-w', '%{http_code}', '--max-time', '25',
               '-r', f'{start}-{start + PROBE_BYTES - 1}', '-A', 'Mozilla/5.0', url]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=40)
        code = result.stdout.strip() or f'exit{result.returncode}'
    except (subprocess.TimeoutExpired, OSError) as error:
        code = f'{type(error).__name__}'
    verdict = '被拒' if code and code[0] in '45' else '可达'
    if log:
        log(f'探测 HTTP {code} -> {verdict}（{note}）', 'warn')
    return code


def download_stream(url, target, expected_size=0, resume_from=0, timeout=300,
                    on_progress=None):
    """Fetch a whole file into `target`, resuming with Range when the server
    honours it. Returns the number of bytes on disk."""
    import urllib.error
    import urllib.request
    headers = {'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'}
    written = resume_from
    if resume_from:
        headers['Range'] = f'bytes={resume_from}-'
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if resume_from and response.status != 206:
                written = 0                       # the server restarted the range
            started = time.time()
            with open(target, 'ab' if written else 'wb') as handle:
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    handle.write(block)
                    written += len(block)
                    if on_progress:
                        on_progress(written, time.time() - started)
    except urllib.error.HTTPError as error:
        raise PikPakError(f'下载被拒: HTTP {error.code}', status=error.code,
                          throttled=error.code in (403, 429, 503)) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise PikPakError(f'下载中断: {type(error).__name__} {error}') from error
    return written


def download_segments(url_of, target, total, connections, wait=time.sleep,
                      stop=lambda: False, log=lambda message, level='info': None):
    """Download `total` bytes with several concurrent segments, then splice them
    into `target` in order.

    Connections open one at a time, and a round that makes no progress gives up on
    segmentation instead of paying another back-off wait for it."""
    seg_dir = _segment_path(target, connections)
    for stale in sorted(_glob.glob(f'{target}.segs*')):
        if stale != seg_dir:
            shutil.rmtree(stale, ignore_errors=True)
    plan = plan_segments(total, connections, seg_dir)
    last_progress = True        # back off only when a round actually got nowhere
    for attempt in range(len(SEGMENT_RETRY_WAITS) + 1):
        wait_seconds = 0 if last_progress else SEGMENT_RETRY_WAITS[attempt - 1]
        missing = [item for item in plan if _segment_have(item) < item['want']]
        if not missing:
            break
        before = sum(_segment_have(item) for item in plan)
        if wait_seconds:
            log(f'{len(missing)} 段未完成，等待 {wait_seconds}s 后第 {attempt} 次退避重试', 'warn')
            wait(wait_seconds)
        elif attempt:
            log(f'{len(missing)} 段继续（上一轮有进展，不退避）', 'debug')
        running = []
        try:
            for position, item in enumerate(missing):
                if position:
                    wait(LAUNCH_STAGGER)
                process = _spawn(item, url_of())
                if process is not None:
                    running.append((item, process))
            for item, process in running:
                while process.poll() is None:
                    if stop():
                        raise PikPakError('已中断（分段进度保留，重跑即续传）')
                    wait(2)
        except PikPakError:
            reap([process for _, process in running])
            raise
        finally:
            reap([process for _, process in running])
        overshot_indices = overshot(plan)
        if overshot_indices and log:
            log(f'段 {overshot_indices} 尾部有越界字节，拼接时将忽略', 'warn')
        gained = sum(_segment_have(item) for item in plan)
        last_progress = gained > before
        if gained <= before:
            # one probe turns "unknown failure" into a decision we can act on: a
            # refusal means stop, a starved lane merely means this round was unlucky
            code = probe_status(url_of(), log=log,
                                note=f'段 {missing[0]["index"] if missing else "-"} 无进展')
            if code[:1] in ('4', '5'):
                raise SegmentRefused(f'分段被拒（HTTP {code}），改用单连接并退避', status=int(code or 0))
    short = [item for item in plan if _segment_have(item) < item['want']]
    if short:
        raise PikPakError('分段多次未完成，暂停本文件: 段 '
                          + ','.join(str(item['index']) for item in short))
    with open(target, 'wb') as out:
        for item in plan:
            remaining = item['want']
            with open(item['path'], 'rb') as handle:
                while remaining > 0:                # never read the overshoot tail
                    block = handle.read(min(8 << 20, remaining))
                    if not block:
                        break
                    out.write(block)
                    remaining -= len(block)
            if remaining:
                raise PikPakError(f"段 {item['index']} 实际字节不足 {item['want'] - remaining}")
    shutil.rmtree(seg_dir, ignore_errors=True)
    return os.path.getsize(target)
