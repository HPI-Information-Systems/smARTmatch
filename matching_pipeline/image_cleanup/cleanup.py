"""Safely unlink fully processed, unmatched auction-artwork image files.

Eligibility is computed from persisted processing and match state, then physical
targets are protected at resolved-path scope so aliases shared by matched or lost
artworks cannot be removed. After deletion (or confirmed absence), affected
``image_file`` rows are marked with ``cleaned_up_at`` and their paths are cleared.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from matching_pipeline.shared.db import connect_db
from matching_pipeline.shared.env import env_repo_root
from shared.image_storage_lock import (
    ImageStorageLockBusy,
    image_storage_lock,
    image_storage_lock_path,
)

logger = logging.getLogger(__name__)

# Stable signed-bigint key for pg_try_advisory_xact_lock ("SMARTCLN").
_CLEANUP_ADVISORY_LOCK_KEY = 0x534D415254434C4E

_INVENTORY_SQL = """
WITH eligible_artwork AS (
    SELECT aa.auction_artwork_id
    FROM auction_artwork aa
    WHERE aa.is_image_matching_processed = true
      AND aa.is_metadata_extraction_processed = true
      AND aa.is_metadata_matching_processed = true
      AND EXISTS (
          SELECT 1
          FROM auction_artwork_image_file linked
          WHERE linked.auction_artwork_id = aa.auction_artwork_id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM auction_artwork_image_file pending
          WHERE pending.auction_artwork_id = aa.auction_artwork_id
            AND (
                pending.is_image_matching_processed = false
                OR pending.is_image_matching_completed_without_error = false
            )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM auction_artwork_image_file linked
          JOIN image_file linked_image
            ON linked_image.image_file_id = linked.image_file_id
          WHERE linked.auction_artwork_id = aa.auction_artwork_id
            AND linked_image.is_embedded = false
      )
      AND NOT EXISTS (
          SELECT 1
          FROM match_score score
          WHERE score.auction_id = aa.auction_artwork_id
      )
)
SELECT
    img.image_file_id,
    img.file_path,
    EXISTS (
        SELECT 1
        FROM auction_artwork_image_file candidate_link
        JOIN eligible_artwork eligible
          ON eligible.auction_artwork_id = candidate_link.auction_artwork_id
        WHERE candidate_link.image_file_id = img.image_file_id
    ) AS has_candidate_auction,
    EXISTS (
        SELECT 1
        FROM lost_artwork_image_file lost_link
        WHERE lost_link.image_file_id = img.image_file_id
    ) AS has_lost_reference,
    EXISTS (
        SELECT 1
        FROM auction_artwork_image_file protected_link
        WHERE protected_link.image_file_id = img.image_file_id
          AND NOT EXISTS (
              SELECT 1
              FROM eligible_artwork eligible
              WHERE eligible.auction_artwork_id = protected_link.auction_artwork_id
          )
    ) AS has_protected_auction_reference,
    EXISTS (
        SELECT 1
        FROM match_score score_reference
        WHERE score_reference.best_image_file_id = img.image_file_id
    ) AS has_direct_score_reference
FROM image_file img
WHERE img.cleaned_up_at IS NULL
  AND img.file_path IS NOT NULL
ORDER BY img.image_file_id
"""


class CleanupAlreadyRunning(RuntimeError):
    """Raised when another cleanup transaction owns the advisory lock."""


class CleanupBlockedByActiveScraper(RuntimeError):
    """Raised when a tracked scraper may be writing the shared image directory."""


class CleanupBlockedByImageWriter(RuntimeError):
    """Raised when any coordinated process is writing the image directory."""


@dataclass(frozen=True)
class ImageUsage:
    image_file_id: int
    file_path: str
    has_candidate_auction: bool
    has_lost_reference: bool
    has_protected_auction_reference: bool
    has_direct_score_reference: bool

    @property
    def protects_file(self) -> bool:
        return (
            self.has_lost_reference
            or self.has_protected_auction_reference
            or self.has_direct_score_reference
        )


@dataclass(frozen=True)
class CleanupResult:
    apply: bool
    inventory_row_count: int
    candidate_image_row_count: int
    candidate_target_count: int
    protected_target_count: int
    would_delete_target_count: int
    deleted_target_count: int
    missing_target_count: int
    unsafe_target_count: int
    failed_target_count: int
    byte_count: int
    cleaned_image_file_ids: tuple[int, ...]
    errors: tuple[str, ...]

    @property
    def cleaned_image_row_count(self) -> int:
        return len(self.cleaned_image_file_ids)

    @property
    def has_failures(self) -> bool:
        return self.unsafe_target_count > 0 or self.failed_target_count > 0


@dataclass(frozen=True)
class _ResolvedUsage:
    usage: ImageUsage
    target: Path | None
    deletion_authorized: bool
    error: str | None


@dataclass(frozen=True)
class _FileOperation:
    status: str
    size: int = 0
    error: str | None = None


def cleanup_unmatched_auction_images(*, image_root: Path, apply: bool) -> CleanupResult:
    """Inspect or delete eligible files while holding a stable DB snapshot.

    Apply mode takes table-level locks before selecting candidates. Those locks
    prevent processing flags, match scores, and image associations from changing
    until filesystem operations and their corresponding cleanup markers commit.
    """

    root = _validated_image_root(image_root, apply=apply)
    if not apply:
        return _cleanup_with_storage_locked(root=root, apply=False)
    try:
        with image_storage_lock(
            root,
            exclusive=True,
            blocking=False,
            create_root=False,
        ):
            return _cleanup_with_storage_locked(root=root, apply=True)
    except ImageStorageLockBusy as exc:
        raise CleanupBlockedByImageWriter(str(exc)) from exc


def _validated_image_root(image_root: Path, *, apply: bool) -> Path:
    root = Path(image_root).expanduser().resolve()
    configured_value = (os.getenv("SMARTMATCH_IMAGES_DIR") or "").strip()
    if configured_value:
        configured = Path(configured_value).expanduser().resolve()
        if root != configured:
            raise ValueError(
                "cleanup image root must equal SMARTMATCH_IMAGES_DIR: "
                f"requested={root} configured={configured}"
            )
    if apply:
        if not root.is_dir():
            raise FileNotFoundError(f"image storage root does not exist: {root}")
        lock_path = image_storage_lock_path(root)
        if not lock_path.is_file() and not any(root.iterdir()):
            raise RuntimeError(
                "image storage root is empty and has no coordination marker; "
                "refusing to mark database paths missing"
            )
    return root


def _cleanup_with_storage_locked(*, root: Path, apply: bool) -> CleanupResult:
    conn = connect_db()
    try:
        with conn.cursor() as cur:
            if apply:
                _lock_cleanup_snapshot(cur)
            else:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cur.execute("SET LOCAL statement_timeout = '15min'")
            usages = _fetch_inventory(cur)
            result = _cleanup_inventory(
                usages,
                image_root=root,
                repo_root=env_repo_root().resolve(),
                apply=apply,
            )
            if apply and result.cleaned_image_file_ids:
                _mark_image_files_cleaned_up(cur, result.cleaned_image_file_ids)
        if apply and result.cleaned_image_file_ids:
            conn.commit()
        else:
            conn.rollback()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mark_image_files_cleaned_up(cur, image_file_ids: Sequence[int]) -> None:
    expected = set(image_file_ids)
    cur.execute(
        """
        UPDATE image_file
        SET file_path = NULL,
            cleaned_up_at = now()
        WHERE image_file_id = ANY(%s)
          AND cleaned_up_at IS NULL
          AND file_path IS NOT NULL
        RETURNING image_file_id
        """,
        (sorted(expected),),
    )
    updated = {int(row[0]) for row in cur.fetchall()}
    if updated != expected:
        missing = sorted(expected - updated)
        unexpected = sorted(updated - expected)
        raise RuntimeError(
            "Could not mark all cleaned image_file rows: "
            f"missing={missing} unexpected={unexpected}"
        )


def _lock_cleanup_snapshot(cur) -> None:
    cur.execute("SET LOCAL lock_timeout = '10s'")
    cur.execute("SET LOCAL statement_timeout = '15min'")
    cur.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
    cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (_CLEANUP_ADVISORY_LOCK_KEY,))
    if not bool(cur.fetchone()[0]):
        raise CleanupAlreadyRunning("another auction image cleanup is running")

    # Every deployed scraper records a running row before it opens its own image
    # transaction. Locking this table closes the start/check race: an existing
    # writer makes cleanup skip, while a new writer cannot start until cleanup
    # has finished unlinking.
    cur.execute("LOCK TABLE scraper_run IN SHARE MODE")
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM scraper_run
            WHERE status = 'running'
              AND finished_at IS NULL
        )
        """
    )
    if bool(cur.fetchone()[0]):
        raise CleanupBlockedByActiveScraper(
            "auction image cleanup is disabled while a scraper is running"
        )

    # SHARE conflicts with every writer's ROW EXCLUSIVE lock but permits readers.
    # image_file is first so untracked/manual writers cannot commit a path or
    # association while aliases are read. Manual writers must still be stopped
    # because table locks cannot serialize filesystem access before their commit.
    cur.execute(
        """
        LOCK TABLE
            image_file,
            match_score,
            auction_artwork_image_file,
            lost_artwork_image_file,
            auction_artwork
        IN SHARE MODE
        """
    )


def _fetch_inventory(cur) -> list[ImageUsage]:
    cur.execute(_INVENTORY_SQL)
    return [
        ImageUsage(
            image_file_id=int(row[0]),
            file_path=str(row[1] if row[1] is not None else ""),
            has_candidate_auction=bool(row[2]),
            has_lost_reference=bool(row[3]),
            has_protected_auction_reference=bool(row[4]),
            has_direct_score_reference=bool(row[5]),
        )
        for row in cur.fetchall()
    ]


def _cleanup_inventory(
    usages: Sequence[ImageUsage],
    *,
    image_root: Path,
    repo_root: Path,
    apply: bool,
) -> CleanupResult:
    resolved = [
        _resolve_usage(usage, image_root=image_root, repo_root=repo_root)
        for usage in usages
    ]
    groups: dict[Path, list[_ResolvedUsage]] = {}
    unresolved_candidates: list[_ResolvedUsage] = []
    for item in resolved:
        if item.target is None:
            if item.usage.has_candidate_auction:
                unresolved_candidates.append(item)
            continue
        groups.setdefault(item.target, []).append(item)

    candidate_groups = [
        (target, rows)
        for target, rows in groups.items()
        if any(row.usage.has_candidate_auction for row in rows)
    ]
    candidate_groups.sort(key=lambda item: str(item[0]))

    protected_target_count = 0
    operations: list[tuple[Path, list[_ResolvedUsage]]] = []
    errors: list[str] = []
    unsafe_target_count = 0

    for target, rows in candidate_groups:
        if any(row.usage.protects_file for row in rows):
            protected_target_count += 1
            continue
        candidate_rows = [row for row in rows if row.usage.has_candidate_auction]
        if not any(row.deletion_authorized for row in candidate_rows):
            unsafe_target_count += 1
            message = _resolution_error(target, candidate_rows)
            errors.append(message)
            logger.error(message)
            continue
        operations.append((target, rows))

    for row in unresolved_candidates:
        unsafe_target_count += 1
        message = row.error or (
            f"Unsafe cleanup path for image_file_id={row.usage.image_file_id}: "
            f"{row.usage.file_path!r}"
        )
        errors.append(message)
        logger.error(message)

    would_delete = 0
    deleted = 0
    missing = 0
    failed = 0
    byte_count = 0
    cleaned_image_file_ids: set[int] = set()

    if operations:
        try:
            root_fd = _open_image_root(image_root)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Image storage root disappeared during cleanup: {image_root}"
            ) from exc
        except OSError as exc:
            failed = len(operations)
            message = f"Cannot open image root {image_root}: {exc}"
            errors.append(message)
            logger.error(message)
            root_fd = None

        if root_fd is not None:
            try:
                for target, _rows in operations:
                    operation = _operate_on_target(
                        root_fd,
                        target.relative_to(image_root),
                        apply=apply,
                    )
                    if operation.status == "regular":
                        would_delete += 1
                        byte_count += operation.size
                    elif operation.status == "deleted":
                        deleted += 1
                        byte_count += operation.size
                        cleaned_image_file_ids.update(
                            row.usage.image_file_id for row in _rows
                        )
                        logger.debug("Deleted unmatched auction image: %s", target)
                    elif operation.status == "missing":
                        missing += 1
                        if apply:
                            cleaned_image_file_ids.update(
                                row.usage.image_file_id for row in _rows
                            )
                    elif operation.status == "unsafe":
                        unsafe_target_count += 1
                        assert operation.error is not None
                        errors.append(f"{target}: {operation.error}")
                        logger.error("Unsafe cleanup target %s: %s", target, operation.error)
                    else:
                        failed += 1
                        assert operation.error is not None
                        errors.append(f"{target}: {operation.error}")
                        logger.error("Could not delete cleanup target %s: %s", target, operation.error)
            finally:
                os.close(root_fd)

    return CleanupResult(
        apply=apply,
        inventory_row_count=len(usages),
        candidate_image_row_count=sum(row.has_candidate_auction for row in usages),
        candidate_target_count=len(candidate_groups) + len(unresolved_candidates),
        protected_target_count=protected_target_count,
        would_delete_target_count=would_delete,
        deleted_target_count=deleted,
        missing_target_count=missing,
        unsafe_target_count=unsafe_target_count,
        failed_target_count=failed,
        byte_count=byte_count,
        cleaned_image_file_ids=tuple(sorted(cleaned_image_file_ids)),
        errors=tuple(errors),
    )


def _resolve_usage(
    usage: ImageUsage,
    *,
    image_root: Path,
    repo_root: Path,
) -> _ResolvedUsage:
    raw_path = usage.file_path
    if raw_path == "":
        return _ResolvedUsage(
            usage,
            None,
            False,
            f"Missing file_path for cleanup candidate image_file_id={usage.image_file_id}",
        )

    if "\x00" in raw_path:
        return _ResolvedUsage(
            usage,
            None,
            False,
            f"NUL byte in file_path for image_file_id={usage.image_file_id}",
        )
    path = Path(raw_path)
    candidates = _db_path_candidates(path, image_root=image_root, repo_root=repo_root)
    selected = next((candidate for candidate in candidates if os.path.lexists(candidate)), candidates[0])
    lexical = Path(os.path.abspath(selected))
    target = lexical.resolve(strict=False)

    try:
        target.relative_to(image_root)
    except ValueError:
        return _ResolvedUsage(
            usage,
            target,
            False,
            f"Path is outside SMARTMATCH_IMAGES_DIR for image_file_id={usage.image_file_id}: {raw_path!r}",
        )
    if lexical != target:
        return _ResolvedUsage(
            usage,
            target,
            False,
            f"Path uses a symlink or non-canonical alias for image_file_id={usage.image_file_id}: {raw_path!r}",
        )
    if target == image_storage_lock_path(image_root):
        return _ResolvedUsage(
            usage,
            target,
            False,
            f"Path is reserved for image-storage coordination: {raw_path!r}",
        )
    return _ResolvedUsage(usage, target, True, None)


def _db_path_candidates(path: Path, *, image_root: Path, repo_root: Path) -> list[Path]:
    if path.is_absolute():
        return [path]
    repo_candidate = repo_root / path
    image_candidate = image_root / path
    try:
        root_relative = image_root.relative_to(repo_root)
    except ValueError:
        root_relative = None
    if root_relative is not None and path.parts[: len(root_relative.parts)] == root_relative.parts:
        ordered: Iterable[Path] = (repo_candidate, image_candidate)
    else:
        ordered = (image_candidate, repo_candidate)

    result: list[Path] = []
    seen: set[str] = set()
    for candidate in ordered:
        key = os.path.abspath(candidate)
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def _resolution_error(target: Path, rows: Sequence[_ResolvedUsage]) -> str:
    details = "; ".join(row.error or row.usage.file_path for row in rows)
    ids = ",".join(str(row.usage.image_file_id) for row in rows)
    return f"No authorized path for cleanup target {target} (candidate image_file_id={ids}): {details}"


def _open_image_root(image_root: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(image_root, flags)


def _operate_on_target(root_fd: int, relative_path: Path, *, apply: bool) -> _FileOperation:
    parts = relative_path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return _FileOperation("unsafe", error="invalid relative target")

    current_fd = os.dup(root_fd)
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            except FileNotFoundError:
                return _FileOperation("missing")
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    return _FileOperation("unsafe", error=f"unsafe parent directory: {exc}")
                return _FileOperation("failed", error=str(exc))
            os.close(current_fd)
            current_fd = next_fd

        filename = parts[-1]
        try:
            target_stat = os.stat(filename, dir_fd=current_fd, follow_symlinks=False)
        except FileNotFoundError:
            return _FileOperation("missing")
        except OSError as exc:
            return _FileOperation("failed", error=str(exc))
        if not stat.S_ISREG(target_stat.st_mode):
            return _FileOperation("unsafe", error="target is not a regular file")
        if not apply:
            return _FileOperation("regular", size=target_stat.st_size)
        try:
            os.unlink(filename, dir_fd=current_fd)
        except FileNotFoundError:
            return _FileOperation("missing")
        except OSError as exc:
            return _FileOperation("failed", error=str(exc))
        return _FileOperation("deleted", size=target_stat.st_size)
    finally:
        os.close(current_fd)
