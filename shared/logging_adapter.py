"""Unified Python logging for every SmartMatch application container.

Application modules should obtain loggers with ``logging.getLogger(__name__)``.
Executable entrypoints call :func:`configure_logging` exactly once.  The root
configuration keeps container output available to Docker while also writing a
multiprocess-safe, date-partitioned file under ``./logs``.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import socket
import stat
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import IO, Iterator, Literal

LOG_LEVEL_ENV = "SMARTMATCH_LOG_LEVEL"
LOG_RETENTION_DAYS_ENV = "SMARTMATCH_LOG_RETENTION_DAYS"
LOG_DIR_ENV = "SMARTMATCH_LOG_DIR"
CONTAINER_NAME_ENV = "SMARTMATCH_CONTAINER_NAME"

_DEFAULT_LOG_LEVEL = "ERROR"
_DEFAULT_RETENTION_DAYS = 30
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LOG_DIR = _PROJECT_ROOT / "logs"
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DATE_SUFFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.txt$")
_LOG_FORMAT = (
    "%(asctime)s | %(levelname)s | container=%(container_name)s | "
    "pid=%(process)d | %(name)s | %(message)s"
)
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

ConsoleMode = Literal["split", "stderr"]


class LogLevel(str, Enum):
    """The only supported user-facing logging modes."""

    ALL = "ALL"
    ERROR = "ERROR"

    @property
    def python_level(self) -> int:
        return logging.DEBUG if self is LogLevel.ALL else logging.ERROR


@dataclass(frozen=True)
class LoggingSettings:
    level: LogLevel
    retention_days: int
    log_dir: Path
    container_name: str


def get_logger(name: str) -> logging.Logger:
    """Return a standard library logger for ``name``."""
    return logging.getLogger(name)


def load_logging_settings() -> LoggingSettings:
    """Load and strictly validate the shared logging environment."""
    raw_level = os.getenv(LOG_LEVEL_ENV, _DEFAULT_LOG_LEVEL).strip().upper()
    try:
        level = LogLevel(raw_level)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in LogLevel)
        raise ValueError(
            f"{LOG_LEVEL_ENV} must be one of {{{allowed}}}; got {raw_level!r}"
        ) from exc

    raw_retention = os.getenv(
        LOG_RETENTION_DAYS_ENV, str(_DEFAULT_RETENTION_DAYS)
    ).strip()
    try:
        retention_days = int(raw_retention)
    except ValueError as exc:
        raise ValueError(
            f"{LOG_RETENTION_DAYS_ENV} must be a positive integer; got {raw_retention!r}"
        ) from exc
    if retention_days <= 0:
        raise ValueError(
            f"{LOG_RETENTION_DAYS_ENV} must be a positive integer; got {raw_retention!r}"
        )

    raw_dir = os.getenv(LOG_DIR_ENV, str(_DEFAULT_LOG_DIR)).strip()
    if not raw_dir:
        raise ValueError(f"{LOG_DIR_ENV} must not be empty")
    log_dir = Path(raw_dir).expanduser()
    if not log_dir.is_absolute():
        log_dir = _PROJECT_ROOT / log_dir
    log_dir = log_dir.resolve()

    container_name = os.getenv(CONTAINER_NAME_ENV, socket.gethostname()).strip()
    if not _CONTAINER_NAME_RE.fullmatch(container_name):
        raise ValueError(
            f"{CONTAINER_NAME_ENV} must match {_CONTAINER_NAME_RE.pattern!r}; "
            f"got {container_name!r}"
        )

    return LoggingSettings(
        level=level,
        retention_days=retention_days,
        log_dir=log_dir,
        container_name=container_name,
    )


class _ContainerContextFilter(logging.Filter):
    def __init__(self, container_name: str) -> None:
        super().__init__()
        self._container_name = container_name

    def filter(self, record: logging.LogRecord) -> bool:
        record.container_name = self._container_name
        return True


class _BelowErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR


def _local_date(timestamp: float | None = None) -> date:
    value = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(value).astimezone().date()


def _seconds_until_next_local_midnight() -> float:
    now = time.time()
    tomorrow = _local_date(now) + timedelta(days=1)
    next_midnight = time.mktime(tomorrow.timetuple())
    return max(1.0, next_midnight - now)


class DailyContainerFileHandler(logging.Handler):
    """Append to one local-date file safely from unrelated Linux processes.

    ``TimedRotatingFileHandler`` is not safe when the scraper and matching
    containers have several unrelated Python processes writing the same file.
    This handler avoids rename races entirely: every process derives the same
    date-stamped path and uses an advisory ``flock`` while appending a record.
    """

    terminator = "\n"

    def __init__(
        self,
        log_dir: Path,
        container_name: str,
        retention_days: int,
    ) -> None:
        super().__init__(level=logging.NOTSET)
        self._log_dir = log_dir
        self._container_name = container_name
        self._retention_days = retention_days
        self._stream: IO[str] | None = None
        self._stream_date: date | None = None
        self._last_cleanup_date: date | None = None
        self._lock_fd: int | None = None
        self._owner_pid = os.getpid()
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: threading.Thread | None = None

        self._log_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        if not self._log_dir.is_dir():
            raise NotADirectoryError(f"Log path is not a directory: {self._log_dir}")

        self._open_lock_file()
        self._perform_daily_maintenance(_local_date())
        self._start_maintenance_thread()

    @property
    def current_log_path(self) -> Path:
        current_date = self._stream_date or _local_date()
        return self._path_for_date(current_date)

    def _path_for_date(self, day: date) -> Path:
        return self._log_dir / f"{self._container_name}_{day.isoformat()}.txt"

    def _open_lock_file(self) -> None:
        lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        lock_flags |= getattr(os, "O_NOFOLLOW", 0)
        self._lock_fd = os.open(
            self._log_dir / f".{self._container_name}.lock",
            lock_flags,
            0o640,
        )

    def _ensure_current_process(self) -> None:
        current_pid = os.getpid()
        if current_pid == self._owner_pid:
            return

        # A fork inherits both descriptors from one open-file description, so
        # flock would not provide parent/child exclusion until the child opens
        # its own lock descriptor. Container workers normally exec, but this
        # lazy reset also makes library-level forks safe.
        if self._stream is not None:
            self._stream.close()
            self._stream = None
            self._stream_date = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
        self._lock_fd = None
        self._owner_pid = current_pid
        self._last_cleanup_date = None
        self._maintenance_stop = threading.Event()
        self._maintenance_thread = None
        self._open_lock_file()
        self._start_maintenance_thread()

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        self._ensure_current_process()
        if self._lock_fd is None:
            raise RuntimeError("Log handler is closed")
        lock_fd = self._lock_fd
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def _start_maintenance_thread(self) -> None:
        if self._maintenance_thread is not None:
            return
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name=f"smartmatch-log-maintenance-{self._container_name}",
            daemon=True,
        )
        self._maintenance_thread.start()

    def _maintenance_loop(self) -> None:
        while not self._maintenance_stop.wait(_seconds_until_next_local_midnight()):
            self.acquire()
            try:
                self._perform_daily_maintenance(_local_date())
            except Exception as exc:
                sys.__stderr__.write(f"SmartMatch log maintenance failed: {exc}\n")
                sys.__stderr__.flush()
            finally:
                self.release()

    def _perform_daily_maintenance(self, today: date) -> None:
        with self._process_lock():
            self._open_stream(today)
            self._delete_expired_files(today)

    def _open_stream(self, day: date) -> None:
        if self._stream is not None and self._stream_date == day:
            return
        if self._stream is not None:
            self._stream.flush()
            self._stream.close()

        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._path_for_date(day), flags, 0o644)
        self._stream = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
        self._stream_date = day

    def _retention_cutoff(self, today: date) -> date:
        # A 30-day setting keeps today's file and the preceding 29 date files.
        return today - timedelta(days=self._retention_days - 1)

    def _delete_expired_files(self, today: date) -> None:
        if self._last_cleanup_date == today:
            return
        cutoff = self._retention_cutoff(today)
        prefix = f"{self._container_name}_"
        for path in self._log_dir.iterdir():
            if not path.name.startswith(prefix):
                continue
            match = _DATE_SUFFIX_RE.fullmatch(path.name[len(prefix) :])
            if match is None:
                continue
            try:
                file_date = date.fromisoformat(match.group(1))
                path_stat = path.stat(follow_symlinks=False)
            except (OSError, ValueError):
                continue
            if file_date >= cutoff or not stat.S_ISREG(path_stat.st_mode):
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self._last_cleanup_date = today

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            today = _local_date()
            record_date = _local_date(record.created)
            # Never open and then immediately unlink a delayed record's file.
            # Records outside the retention window remain visible in today's
            # file while preserving their original formatted timestamp.
            if record_date < self._retention_cutoff(today):
                record_date = today
            with self._process_lock():
                self._open_stream(record_date)
                self._delete_expired_files(today)
                assert self._stream is not None
                self._stream.write(message + self.terminator)
                self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self._ensure_current_process()
        self._maintenance_stop.set()
        maintenance_thread = self._maintenance_thread
        if (
            maintenance_thread is not None
            and maintenance_thread is not threading.current_thread()
        ):
            maintenance_thread.join(timeout=2.0)
        self._maintenance_thread = None

        self.acquire()
        try:
            if self._lock_fd is not None:
                with self._process_lock():
                    if self._stream is not None:
                        self._stream.flush()
                        self._stream.close()
                        self._stream = None
                os.close(self._lock_fd)
                self._lock_fd = None
        finally:
            self.release()
            super().close()


def _handler(
    handler: logging.Handler,
    *,
    settings: LoggingSettings,
    formatter: logging.Formatter,
) -> logging.Handler:
    handler.setLevel(settings.level.python_level)
    handler.setFormatter(formatter)
    handler.addFilter(_ContainerContextFilter(settings.container_name))
    return handler


def _install_exception_hooks() -> None:
    def log_uncaught_exception(
        exception_type: type[BaseException],
        exception: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        if issubclass(exception_type, KeyboardInterrupt):
            sys.__excepthook__(exception_type, exception, traceback)
            return
        logging.getLogger("smartmatch.uncaught").critical(
            "Uncaught exception",
            exc_info=(exception_type, exception, traceback),
        )

    def log_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        logging.getLogger("smartmatch.uncaught").critical(
            "Uncaught exception in thread %s",
            args.thread.name if args.thread is not None else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = log_uncaught_exception
    threading.excepthook = log_uncaught_thread_exception


def configure_logging(*, console_mode: ConsoleMode = "split") -> LoggingSettings:
    """Replace root handlers with the shared console and daily-file contract.

    ``console_mode='stderr'`` is reserved for Supervisor event listeners whose
    stdout is a control protocol. All other entrypoints use split stdout/stderr.
    """
    if console_mode not in {"split", "stderr"}:
        raise ValueError(f"Unsupported console mode: {console_mode!r}")

    settings = load_logging_settings()
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    handlers: list[logging.Handler] = []

    if console_mode == "split":
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.addFilter(_BelowErrorFilter())
        handlers.append(
            _handler(stdout_handler, settings=settings, formatter=formatter)
        )
    stderr_handler = _handler(
        logging.StreamHandler(sys.stderr),
        settings=settings,
        formatter=formatter,
    )
    if console_mode == "split":
        stderr_handler.setLevel(logging.ERROR)
    handlers.append(stderr_handler)
    handlers.append(
        _handler(
            DailyContainerFileHandler(
                settings.log_dir,
                settings.container_name,
                settings.retention_days,
            ),
            settings=settings,
            formatter=formatter,
        )
    )

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
        existing.close()
    root.setLevel(settings.level.python_level)
    for handler in handlers:
        root.addHandler(handler)

    logging.captureWarnings(True)
    _install_exception_hooks()
    return settings
