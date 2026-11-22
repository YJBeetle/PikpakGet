"""One bounded retry policy and transport classification for all network paths."""
import http.client
import time
import urllib.error

from .errors import PikPakError

NETWORK_RETRY_WAITS = (5, 15, 45, 120, 240)
THROTTLE_BACKOFF = (30, 120, 480, 900)
REQUEST_ATTEMPTS = 5
MAX_CONSECUTIVE_FAILURES = 3
TRANSPORT_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)


class RecoveryState:
    """Command-wide failure streak, shared across accounts and series."""
    def __init__(self):
        self.failures = 0
        self.exhausted = False

    def failed(self):
        self.failures += 1
        self.exhausted = self.failures >= MAX_CONSECUTIVE_FAILURES

    def completed_file(self):
        self.failures = 0
        self.exhausted = False


def check_stop(stop, action=None):
    if stop():
        raise PikPakError('已中断（进度和登录状态已保留）', action=action)


def interruptible_wait(seconds, stop, sleep=None, interval=0.5):
    sleep = sleep or time.sleep
    check_stop(stop)
    while seconds > 0:
        step = min(interval, seconds)
        sleep(step)
        seconds -= step
        check_stop(stop)


def transport_error(error, action=None):
    return PikPakError(f'网络连接异常: {type(error).__name__}: {error}',
                      action=action, network=True)


def require_complete(written, expected):
    if expected and written != expected:
        raise PikPakError(f'下载不完整 {written}/{expected}', action='download',
                          network=written < expected)


def retry(operation, *, log, stop, wait, attempts=REQUEST_ATTEMPTS):
    """Retry only transient, replay-safe failures; nested exhaustion is final."""
    for attempt in range(attempts):
        check_stop(stop)
        try:
            result = operation()
            check_stop(stop)
            return result
        except PikPakError as error:
            check_stop(stop)
            if not error.transient or not error.replay_safe or error.retry_exhausted:
                raise
            if attempt + 1 == attempts:
                error.retry_exhausted = True
                raise
            if error.retry_after is not None:
                delay = error.retry_after
            else:
                delays = THROTTLE_BACKOFF if error.throttled else NETWORK_RETRY_WAITS
                delay = delays[min(attempt, len(delays) - 1)]
            log(f'请求暂时失败：{error}；{delay}s 后重试（{attempt + 2}/{attempts}）', 'warn')
            wait(delay)
    raise ValueError('attempts must be positive')
