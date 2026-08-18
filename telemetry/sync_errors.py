"""Synchronization error taxonomy and retry classification."""

from http.client import IncompleteRead
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler

from psycopg import InterfaceError, OperationalError


class SyncError(RuntimeError):
    """Base class for classified telemetry synchronization failures."""


class TerminalSyncError(SyncError):
    """A retry cannot succeed without changing configuration or source data."""


class TransientSyncError(SyncError):
    """A bounded retry may succeed without operator intervention."""


class SyncHttpError(SyncError):
    def __init__(
        self,
        status: int,
        message: str | None = None,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        detail = f": {message}" if message else ""
        super().__init__(f"Telemetry sync endpoint returned HTTP {status}{detail}")
        self.status = status
        self.retry_after_seconds = retry_after_seconds

    @property
    def retryable(self) -> bool:
        return self.status in {408, 425, 429} or 500 <= self.status < 600


class SyncProtocolError(TerminalSyncError):
    pass


class AsyncApplyError(TerminalSyncError):
    pass


class SyncWorkspaceLimitError(TerminalSyncError):
    pass


class UnsendableClosureError(TerminalSyncError):
    pass


class _ClosureMaterializationLimit(RuntimeError):
    def __init__(self, *, label: str, attempted_bytes: int, max_bytes: int) -> None:
        super().__init__(
            f"Telemetry {label} materialization would use an estimated "
            f"{attempted_bytes} bytes; maximum is {max_bytes}"
        )
        self.label = label
        self.attempted_bytes = attempted_bytes
        self.max_bytes = max_bytes


class SourceSnapshotChanged(TransientSyncError):
    pass


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def is_transient_sync_failure(exc: BaseException) -> bool:
    if isinstance(exc, TransientSyncError):
        return True
    if isinstance(exc, SyncHttpError):
        return exc.retryable
    return isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            IncompleteRead,
            URLError,
            OperationalError,
            InterfaceError,
        ),
    )


def is_terminal_sync_failure(exc: BaseException) -> bool:
    if isinstance(exc, TerminalSyncError):
        return True
    if isinstance(exc, SyncHttpError):
        return not exc.retryable
    return not is_transient_sync_failure(exc)
