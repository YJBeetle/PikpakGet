"""Failure categories shared by requests, transfers, and task scheduling."""


class PikPakError(RuntimeError):
    def __init__(self, message, status=None, code=None, action=None, throttled=False,
                 network=False, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.action = action
        self.throttled = throttled
        self.network = network
        self.retry_after = retry_after
        self.retry_exhausted = False

    @property
    def transient(self):
        return (self.network or self.throttled or self.retry_after is not None
                or self.status == 429 or (self.status is not None and self.status >= 500))

    @property
    def replay_safe(self):
        return True

    def __str__(self):
        detail = ', '.join(part for part in (
            f'HTTP {self.status}' if self.status is not None else '',
            f'code={self.code}' if self.code is not None else '',
            f'action={self.action}' if self.action else '') if part)
        base = super().__str__()
        return f'{base} [{detail}]' if detail else base


class TrafficCapped(PikPakError):
    """A confirmed downstream cap belongs to the account cooldown scheduler."""
    @property
    def transient(self):
        return False


class MutationOutcomeUnknown(PikPakError):
    """A cloud write may have taken effect; reconcile before submitting it again."""
    @property
    def replay_safe(self):
        return False


class RestoreOutcomeUnknown(MutationOutcomeUnknown):
    """A restore may have succeeded despite losing its response."""
