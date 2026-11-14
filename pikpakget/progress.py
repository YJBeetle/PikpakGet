"""Live transfer display, with sparse plain-text updates outside a terminal."""
import threading
import time
from collections import deque


class TransferProgress:
    def __init__(self, log, total, initial=0):
        self.log = log
        self.total = total
        self.current = self.initial = initial
        self.started = time.monotonic()
        self.samples = deque([(self.started, initial)])
        self.last = 0
        self.done = threading.Event()
        self.thread = None

    def update(self, written, elapsed=0):
        self.current = written

    def render(self, force=False):
        from .pipeline import human
        now = time.monotonic()
        interval = 1 if self.log.is_terminal else 30
        if not force and now - self.last < interval:
            return
        self.last = now
        self.samples.append((now, self.current))
        while len(self.samples) > 2 and self.samples[1][0] <= now - 5:
            self.samples.popleft()
        since, before = self.samples[0]
        speed = max(self.current - before, 0) / max(now - since, 0.001)
        fraction = min(self.current / self.total, 1) if self.total else 0
        eta = max(self.total - self.current, 0) / speed if speed else None
        remaining = f'{int(eta) // 60}分{int(eta) % 60:02d}秒' if eta is not None else '未知'
        bar = '#' * int(fraction * 20) + '-' * (20 - int(fraction * 20))
        message = (f'下载 [{bar}] {fraction:5.1%} '
                   f'{human(self.current)}/{human(self.total)} '
                   f'{human(speed)}/s 剩余 {remaining}')
        self.log.transfer(message)

    def __enter__(self):
        self.render(True)
        if not self.log.quiet:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self

    def _run(self):
        while not self.done.wait(1):
            self.render()

    def __exit__(self, *exc):
        self.done.set()
        if self.thread:
            self.thread.join()
        self.render(True)
        self.log.clear_transfer()
