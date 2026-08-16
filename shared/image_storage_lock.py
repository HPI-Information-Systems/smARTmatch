"""Cross-process lock for the shared image storage directory."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_LOCK_FILENAME = ".smartmatch-image-storage.lock"


class ImageStorageLockBusy(RuntimeError):
    """Raised when a nonblocking image-storage lock cannot be acquired."""


def image_storage_coordination_root(image_root: Path) -> Path:
    """Return the common lock root for a configured root or its subdirectories."""

    requested = Path(image_root).expanduser().resolve()
    configured_value = (os.getenv("SMARTMATCH_IMAGES_DIR") or "").strip()
    if not configured_value:
        return requested
    configured = Path(configured_value).expanduser().resolve()
    try:
        requested.relative_to(configured)
    except ValueError:
        return requested
    return configured


def image_storage_lock_path(image_root: Path) -> Path:
    """Return the canonical lock-file path for an image storage root."""

    return image_storage_coordination_root(image_root) / _LOCK_FILENAME


@contextmanager
def image_storage_lock(
    image_root: Path,
    *,
    exclusive: bool,
    blocking: bool = True,
    create_root: bool = True,
) -> Iterator[None]:
    """Hold a process-wide shared-writer or exclusive-cleanup filesystem lock.

    The lock file lives inside the bind-mounted image root, so scraper and
    matching containers coordinate through the same inode. Writers hold a
    shared lock for their complete run; cleanup holds an exclusive lock from
    before its database snapshot through the last unlink.
    """

    root = image_storage_coordination_root(image_root)
    if create_root:
        root.mkdir(parents=True, exist_ok=True)
    elif not root.is_dir():
        raise FileNotFoundError(f"image storage root does not exist: {root}")
    descriptor = os.open(
        image_storage_lock_path(root),
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as exc:
            raise ImageStorageLockBusy(
                f"image storage is busy: {root}"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
