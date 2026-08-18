"""Incremental disk, page, transfer, and materialization budgets."""

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from telemetry.sync_constants import (
    _MATERIALIZATION_FIXED_OVERHEAD_BYTES,
    _MATERIALIZATION_ROW_OVERHEAD_BYTES,
    MAX_SYNC_OPERATION_PAGES,
    MAX_SYNC_TRANSFER_BYTES,
    MAX_SYNC_WORKSPACE_BYTES,
    MIN_SYNC_FILESYSTEM_FREE_BYTES,
)
from telemetry.sync_errors import (
    SyncWorkspaceLimitError,
    _ClosureMaterializationLimit,
)


@dataclass
class TransferBudget:
    max_bytes: int = MAX_SYNC_TRANSFER_BYTES
    attempted_bytes: int = 0

    def debit(self, size: int) -> None:
        if self.attempted_bytes + size > self.max_bytes:
            raise SyncWorkspaceLimitError(
                "Telemetry sync attempted transfer would exceed "
                f"{self.max_bytes} compressed bytes "
                f"(attempted={self.attempted_bytes}, next={size})"
            )
        self.attempted_bytes += size


@dataclass
class _ClosureMaterializationBudget:
    max_bytes: int
    estimated_bytes: int = _MATERIALIZATION_FIXED_OVERHEAD_BYTES

    def reserve(self, payload_bytes: int, row_count: int, *, label: str) -> None:
        additional = max(0, int(payload_bytes)) + (
            max(0, int(row_count)) * _MATERIALIZATION_ROW_OVERHEAD_BYTES
        )
        attempted = self.estimated_bytes + additional
        if attempted > self.max_bytes:
            raise _ClosureMaterializationLimit(
                label=label,
                attempted_bytes=attempted,
                max_bytes=self.max_bytes,
            )
        self.estimated_bytes = attempted


@dataclass
class WorkspaceBudget:
    root: Path
    max_bytes: int = MAX_SYNC_WORKSPACE_BYTES
    max_pages: int = MAX_SYNC_OPERATION_PAGES
    page_count: int = 0
    _used_bytes: int = field(init=False, repr=False)
    _tracked_file_sizes: dict[Path, int] = field(
        init=False, repr=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        # The workspace is worker-owned, so all subsequent persistent writes are
        # either reserved pages or explicitly tracked files such as the catalog.
        self._used_bytes = _directory_size(self.root)

    def track_file(self, path: Path) -> None:
        """Start tracking a mutable file already included in the initial total."""
        self._tracked_file_sizes[path] = self._file_size(path)

    def refresh_file(self, path: Path) -> None:
        """Account for growth of a tracked file without rescanning the workspace."""
        if path not in self._tracked_file_sizes:
            raise ValueError(f"Telemetry workspace file is not tracked: {path}")
        current_size = self._file_size(path)
        self._used_bytes += current_size - self._tracked_file_sizes[path]
        self._tracked_file_sizes[path] = current_size

    def release(self, size: int) -> None:
        """Release bytes for files that have been removed from the workspace."""
        if size < 0 or size > self._used_bytes:
            raise ValueError(f"Invalid telemetry workspace release: {size}")
        self._used_bytes -= size

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise SyncWorkspaceLimitError(
                f"Could not measure telemetry sync workspace file {path}: {exc}"
            ) from exc

    def ensure_page_capacity(self, *, reserved_pages: int = 0) -> None:
        if self.page_count + 1 + reserved_pages > self.max_pages:
            raise SyncWorkspaceLimitError(
                f"Telemetry sync would exceed {self.max_pages} operation pages"
            )
        if self._used_bytes >= self.max_bytes:
            raise SyncWorkspaceLimitError(
                f"Telemetry sync workspace uses {self._used_bytes} bytes; maximum is "
                f"{self.max_bytes}"
            )
        free = shutil.disk_usage(self.root).free
        if free <= MIN_SYNC_FILESYSTEM_FREE_BYTES:
            raise SyncWorkspaceLimitError(
                "Telemetry sync breached the filesystem free-space reserve "
                f"(free={free}, reserve={MIN_SYNC_FILESYSTEM_FREE_BYTES})"
            )

    def next_page_materialization_limit(self, requested_bytes: int) -> int:
        self.ensure_page_capacity()
        free = shutil.disk_usage(self.root).free
        available = min(
            self.max_bytes - self._used_bytes,
            free - MIN_SYNC_FILESYSTEM_FREE_BYTES,
        )
        if available <= _MATERIALIZATION_FIXED_OVERHEAD_BYTES:
            raise SyncWorkspaceLimitError(
                "Telemetry sync workspace has insufficient capacity for "
                f"another page (available={max(0, available)})"
            )
        return min(requested_bytes, available)

    def reserve_page(self, size: int) -> None:
        self.ensure_page_capacity()
        if self._used_bytes + size > self.max_bytes:
            raise SyncWorkspaceLimitError(
                "Telemetry sync workspace would exceed "
                f"{self.max_bytes} bytes "
                f"(used={self._used_bytes}, next_page={size})"
            )
        free = shutil.disk_usage(self.root).free
        if free - size < MIN_SYNC_FILESYSTEM_FREE_BYTES:
            raise SyncWorkspaceLimitError(
                "Telemetry sync would breach the filesystem free-space reserve "
                f"(free={free}, next_page={size}, "
                f"reserve={MIN_SYNC_FILESYSTEM_FREE_BYTES})"
            )
        self._used_bytes += size
        self.page_count += 1

    def ensure_within_limit(self) -> None:
        if self._used_bytes > self.max_bytes:
            raise SyncWorkspaceLimitError(
                f"Telemetry sync workspace uses {self._used_bytes} bytes; maximum is "
                f"{self.max_bytes}"
            )
        free = shutil.disk_usage(self.root).free
        if free < MIN_SYNC_FILESYSTEM_FREE_BYTES:
            raise SyncWorkspaceLimitError(
                "Telemetry sync breached the filesystem free-space reserve "
                f"(free={free}, reserve={MIN_SYNC_FILESYSTEM_FREE_BYTES})"
            )


def _directory_size(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    except OSError as exc:
        raise SyncWorkspaceLimitError(
            f"Could not measure telemetry sync workspace: {exc}"
        ) from exc
    return total
