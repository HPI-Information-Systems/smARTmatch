"""Locked synchronization workspace creation and stale-spool cleanup."""

import fcntl
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from telemetry.sync_constants import (
    _LEGACY_STALE_SPOOL_SECONDS,
    _SYNC_SPOOL_DIRECTORY,
    logger,
)


def sync_spool_root() -> Path:
    root = Path(tempfile.gettempdir()) / _SYNC_SPOOL_DIRECTORY
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


@contextmanager
def sync_workspace() -> Iterator[Path]:
    """Create a worker-owned workspace protected from concurrent scavengers."""
    with tempfile.TemporaryDirectory(prefix="sync-", dir=sync_spool_root()) as temp_dir:
        root = Path(temp_dir)
        lock_path = root / ".workspace.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        os.write(lock_fd, f"{os.getpid()}\n".encode("ascii"))
        try:
            yield root
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def cleanup_stale_sync_spools() -> None:
    """Remove unlocked workspaces and old pre-lock workspaces left by dead workers."""
    for path in sync_spool_root().glob("sync-*"):
        if not path.is_dir():
            continue
        lock_fd: int | None = None
        try:
            lock_fd = os.open(path / ".workspace.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            shutil.rmtree(path)
        except OSError:
            logger.warning("Could not remove stale telemetry spool %s", path)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)

    now = time.time()
    for path in Path(tempfile.gettempdir()).glob("smartmatch-sync-*"):
        if not path.is_dir():
            continue
        try:
            if now - path.stat().st_mtime < _LEGACY_STALE_SPOOL_SECONDS:
                continue
            shutil.rmtree(path)
        except OSError:
            logger.warning("Could not remove stale legacy telemetry spool %s", path)
