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


def _segment_have(item):
    return os.path.getsize(item['path']) if os.path.exists(item['path']) else 0


def _spawn(item, url):
    """Fetch one segment's remaining bytes in append mode, so a retry continues
    from the segment's current size instead of restarting it."""
    handle = open(item['path'], 'ab')
    command = ['curl', '-sS', '--fail', '--speed-limit', str(STALL_LIMIT),
               '--speed-time', str(STALL_SECONDS), '--retry', '2', '--retry-delay', '5',
               '-r', f"{item['start'] + item['have']}-{item['end']}",
               '-o', '-', '-A', 'Mozilla/5.0', url]
    try:
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.DEVNULL)
    finally:
        handle.close()
    return process


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
    for attempt, wait_seconds in enumerate((0,) + SEGMENT_RETRY_WAITS):
        missing = [item for item in plan if _segment_have(item) < item['want']]
        if not missing:
            break
        before = sum(_segment_have(item) for item in plan)
        if wait_seconds:
            log(f'{len(missing)} 段未完成，等待 {wait_seconds}s 后第 {attempt} 次重试', 'warn')
            wait(wait_seconds)
        running = []
        try:
            for position, item in enumerate(missing):
                if position:
                    wait(LAUNCH_STAGGER)
                running.append((item, _spawn(item, url_of())))
            for item, process in running:
                while process.poll() is None:
                    if stop():
                        raise PikPakError('已中断（分段进度保留，重跑即续传）')
                    wait(2)
        except PikPakError:
            for _, process in running:
                process.terminate()
            raise
        finally:
            for _, process in running:
                if process.poll() is None:
                    process.terminate()
        gained = sum(_segment_have(item) for item in plan)
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
            with open(item['path'], 'rb') as handle:
                shutil.copyfileobj(handle, out, 8 << 20)
    shutil.rmtree(seg_dir, ignore_errors=True)
    return os.path.getsize(target)
