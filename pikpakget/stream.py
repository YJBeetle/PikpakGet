"""Byte transfer: one resumable stream, or several concurrent ranged curl
segments spliced together afterwards.

The CDN throttles each connection (~0.5-0.7 MB/s on a free account) but the
account is shaped as well, so extra segments buy a sublinear gain. Both paths
survive interruption: the stream keeps a `.part` file, segments keep their own
partial bytes, and either resumes where it stopped.
"""
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
                written = 0                       # restarted from scratch
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


def _run_segment(item, url, env=None):
    """One ranged curl write in append mode: `-o -` plus an already-open handle,
    so a retry continues from the segment's current size."""
    handle = open(item['path'], 'ab')
    command = ['curl', '-sS', '--fail', '--speed-limit', str(STALL_LIMIT),
               '--speed-time', str(STALL_SECONDS), '--retry', '2', '--retry-delay', '5',
               '-r', f"{item['start'] + item['have']}-{item['end']}",
               '-o', '-', '-A', 'Mozilla/5.0', url]
    try:
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.DEVNULL, env=env)
    finally:
        handle.close()
    return process


def download_segments(url_of, target, total, connections, wait=time.sleep,
                      stop=lambda: False, log=lambda message, level='info': None,
                      seg_dir=None):
    """Download `total` bytes with several concurrent segments, then splice them
    into `target` in order.

    Refusals are treated as the dangerous case: rounds are separated by minutes
    and connections are opened one at a time, because rapidly repeating a
    rejected request is what gets an account flagged, not the traffic itself."""
    seg_dir = seg_dir or f'{target}.segs'
    plan = plan_segments(total, connections, seg_dir)
    for attempt, wait_seconds in enumerate((0,) + SEGMENT_RETRY_WAITS):
        missing = [item for item in plan if _segment_have(item) < item['want']]
        if not missing:
            break
        if wait_seconds:
            log(f'{len(missing)} 个分段未完成，等待 {wait_seconds}s 后第 {attempt} 次重试', 'warn')
            wait(wait_seconds)
        running = []
        try:
            for position, item in enumerate(missing):
                if position:
                    wait(LAUNCH_STAGGER)
                running.append((item, _run_segment(item, url_of())))
            for item, process in running:
                while process.poll() is None:
                    if stop():
                        process.terminate()
                        raise PikPakError('已中断（分段进度保留，重跑即续传）')
                    wait(2)
        finally:
            for _, process in running:
                if process.poll() is None:
                    process.terminate()
            for item, _ in running:
                item['have'] = _segment_have(item)
    short = [item for item in plan if _segment_have(item) < item['want']]
    if short:
        raise PikPakError('分段多次未完成，暂停该文件（疑似限流）: 段 '
                          + ','.join(str(item['index']) for item in short))
    with open(target, 'wb') as out:
        for item in plan:
            with open(item['path'], 'rb') as handle:
                shutil.copyfileobj(handle, out, 8 << 20)
    shutil.rmtree(seg_dir, ignore_errors=True)
    return os.path.getsize(target)
