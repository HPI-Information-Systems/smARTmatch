"""Safely remove fully processed, unmatched auction-artwork image files.

Eligible files are durably journaled and atomically renamed into an internal,
same-filesystem quarantine before database markers commit. After commit they are
purged. A later cleanup run restores pre-commit moves or finishes post-commit
purges, preventing an interrupted cross-system operation from losing file bytes.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from uuid import uuid4
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
_QUARANTINE_DIR_NAME = ".smartmatch-cleanup-quarantine"
_QUARANTINE_JOURNAL_VERSION = 1
_MAX_JOURNAL_BYTES = 64 * 1024

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


class CleanupPurgePending(RuntimeError):
    """Raised after DB commit when quarantined bytes still require purging."""


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
class _QuarantinedFile:
    operation_id: str
    journal_name: str
    staged_name: str
    relative_path: Path
    image_file_ids: tuple[int, ...]
    size: int


@dataclass(frozen=True)
class _FileOperation:
    status: str
    size: int = 0
    error: str | None = None
    quarantined: _QuarantinedFile | None = None


@dataclass(frozen=True)
class _CleanupInventoryOutcome:
    result: CleanupResult
    quarantined_files: tuple[_QuarantinedFile, ...]


def cleanup_unmatched_auction_images(*, image_root: Path, apply: bool) -> CleanupResult:
    """Inspect or delete eligible files while holding a stable DB snapshot.

    Apply mode takes table-level locks before selecting candidates. Those locks
    prevent processing flags, match scores, and image associations from changing
    until filesystem quarantine moves and their cleanup markers commit.
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
    quarantined_files: tuple[_QuarantinedFile, ...] = ()
    commit_attempted = False
    commit_succeeded = False
    recovery_completed = False
    try:
        with conn.cursor() as cur:
            if apply:
                _lock_cleanup_snapshot(cur)
                _recover_quarantine(cur, root)
                recovery_completed = True
            else:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cur.execute("SET LOCAL statement_timeout = '15min'")
            usages = _fetch_inventory(cur)
            outcome = _cleanup_inventory(
                usages,
                image_root=root,
                repo_root=env_repo_root().resolve(),
                apply=apply,
            )
            result = outcome.result
            quarantined_files = outcome.quarantined_files
            if apply and result.cleaned_image_file_ids:
                _mark_image_files_cleaned_up(cur, result.cleaned_image_file_ids)
        if apply and result.cleaned_image_file_ids:
            commit_attempted = True
            conn.commit()
            commit_succeeded = True
            try:
                _purge_quarantined_files(root, quarantined_files)
            except Exception as exc:
                raise CleanupPurgePending(
                    "Cleanup markers committed; quarantined files remain pending purge"
                ) from exc
        else:
            conn.rollback()
        return result
    except Exception:
        if not commit_succeeded:
            try:
                conn.rollback()
            except Exception:
                logger.exception("Could not roll back cleanup database transaction")
        if apply and recovery_completed and not commit_attempted:
            try:
                _restore_uncommitted_quarantine(root)
            except Exception:
                logger.exception(
                    "Could not restore quarantined cleanup files; "
                    "the next cleanup run must recover them"
                )
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
) -> _CleanupInventoryOutcome:
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
    quarantined_files: list[_QuarantinedFile] = []

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
            quarantine_fd = None
            try:
                if apply:
                    quarantine_fd = _open_quarantine(root_fd, create=True)
                for target, _rows in operations:
                    image_file_ids = tuple(
                        sorted(row.usage.image_file_id for row in _rows)
                    )
                    operation = _operate_on_target(
                        root_fd,
                        target.relative_to(image_root),
                        apply=apply,
                        quarantine_fd=quarantine_fd,
                        image_file_ids=image_file_ids,
                    )
                    if operation.status == "regular":
                        would_delete += 1
                        byte_count += operation.size
                    elif operation.status == "deleted":
                        deleted += 1
                        byte_count += operation.size
                        cleaned_image_file_ids.update(image_file_ids)
                        assert operation.quarantined is not None
                        quarantined_files.append(operation.quarantined)
                        logger.debug("Quarantined unmatched auction image: %s", target)
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
                if quarantine_fd is not None:
                    os.close(quarantine_fd)
                    _remove_quarantine_dir_if_empty(root_fd)
                os.close(root_fd)

    result = CleanupResult(
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
    return _CleanupInventoryOutcome(result, tuple(quarantined_files))


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
        relative_target = target.relative_to(image_root)
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
    if relative_target.parts and relative_target.parts[0] == _QUARANTINE_DIR_NAME:
        return _ResolvedUsage(
            usage,
            target,
            False,
            f"Path is reserved for cleanup quarantine: {raw_path!r}",
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


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    return flags | getattr(os, "O_NOFOLLOW", 0)


def _open_quarantine(root_fd: int, *, create: bool) -> int | None:
    if create:
        try:
            os.mkdir(_QUARANTINE_DIR_NAME, mode=0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
    try:
        return os.open(
            _QUARANTINE_DIR_NAME,
            _directory_open_flags(),
            dir_fd=root_fd,
        )
    except FileNotFoundError:
        if not create:
            return None
        raise
    except OSError as exc:
        raise RuntimeError("Cleanup quarantine is not a safe directory") from exc


def _remove_quarantine_dir_if_empty(root_fd: int) -> None:
    quarantine_fd = _open_quarantine(root_fd, create=False)
    if quarantine_fd is None:
        return
    try:
        if os.listdir(quarantine_fd):
            return
    finally:
        os.close(quarantine_fd)
    try:
        os.rmdir(_QUARANTINE_DIR_NAME, dir_fd=root_fd)
        os.fsync(root_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
            raise


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise OSError("short write while creating cleanup journal")
        offset += written


def _unlink_if_exists(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _create_quarantine_journal(
    quarantine_fd: int,
    *,
    relative_path: Path,
    image_file_ids: tuple[int, ...],
    size: int,
) -> _QuarantinedFile:
    operation_id = uuid4().hex
    journal_name = f"{operation_id}.json"
    temporary_name = f".{operation_id}.json.tmp"
    staged_name = f"{operation_id}.data"
    if _regular_entry_exists(quarantine_fd, staged_name):
        raise RuntimeError(f"Cleanup quarantine collision: {staged_name}")
    quarantined = _QuarantinedFile(
        operation_id,
        journal_name,
        staged_name,
        relative_path,
        image_file_ids,
        size,
    )
    payload = json.dumps(
        {
            "version": _QUARANTINE_JOURNAL_VERSION,
            "operation_id": operation_id,
            "journal_name": journal_name,
            "staged_name": staged_name,
            "relative_path": relative_path.as_posix(),
            "image_file_ids": list(image_file_ids),
            "size": size,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > _MAX_JOURNAL_BYTES:
        raise RuntimeError(
            "Cleanup journal would exceed the supported size for "
            f"{len(image_file_ids)} image_file rows"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    journal_fd = os.open(temporary_name, flags, 0o600, dir_fd=quarantine_fd)
    try:
        _write_all(journal_fd, payload)
        os.fsync(journal_fd)
    finally:
        os.close(journal_fd)
    try:
        os.rename(
            temporary_name,
            journal_name,
            src_dir_fd=quarantine_fd,
            dst_dir_fd=quarantine_fd,
        )
        os.fsync(quarantine_fd)
    except Exception:
        _unlink_if_exists(quarantine_fd, temporary_name)
        _unlink_if_exists(quarantine_fd, journal_name)
        raise
    return quarantined


def _stage_target(
    source_directory_fd: int,
    filename: str,
    target_fd: int,
    target_stat: os.stat_result,
    quarantine_fd: int,
    *,
    relative_path: Path,
    image_file_ids: tuple[int, ...],
    size: int,
) -> _FileOperation:
    quarantined = _create_quarantine_journal(
        quarantine_fd,
        relative_path=relative_path,
        image_file_ids=image_file_ids,
        size=size,
    )
    try:
        os.rename(
            filename,
            quarantined.staged_name,
            src_dir_fd=source_directory_fd,
            dst_dir_fd=quarantine_fd,
        )
    except FileNotFoundError:
        _unlink_if_exists(quarantine_fd, quarantined.journal_name)
        os.fsync(quarantine_fd)
        return _FileOperation("missing")
    except OSError as exc:
        _unlink_if_exists(quarantine_fd, quarantined.journal_name)
        os.fsync(quarantine_fd)
        return _FileOperation("failed", error=str(exc))
    staged_stat = os.stat(
        quarantined.staged_name,
        dir_fd=quarantine_fd,
        follow_symlinks=False,
    )
    opened_stat = os.fstat(target_fd)
    expected_identity = (
        target_stat.st_dev,
        target_stat.st_ino,
        target_stat.st_size,
        target_stat.st_mtime_ns,
    )
    staged_identity = (
        staged_stat.st_dev,
        staged_stat.st_ino,
        staged_stat.st_size,
        staged_stat.st_mtime_ns,
    )
    opened_identity = (
        opened_stat.st_dev,
        opened_stat.st_ino,
        opened_stat.st_size,
        opened_stat.st_mtime_ns,
    )
    if (
        not stat.S_ISREG(staged_stat.st_mode)
        or staged_identity != expected_identity
        or opened_identity != expected_identity
    ):
        try:
            os.stat(filename, dir_fd=source_directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(
                quarantined.staged_name,
                filename,
                src_dir_fd=quarantine_fd,
                dst_dir_fd=source_directory_fd,
            )
            os.fsync(source_directory_fd)
            os.fsync(quarantine_fd)
            _unlink_if_exists(quarantine_fd, quarantined.journal_name)
            os.fsync(quarantine_fd)
            return _FileOperation(
                "unsafe", error="cleanup target changed before quarantine move"
            )
        raise RuntimeError(
            f"Cleanup target changed and destination was replaced: {relative_path}"
        )
    try:
        # Persist the destination entry before the source removal so a crash
        # cannot durably lose both names for the same inode.
        os.fsync(quarantine_fd)
        os.fsync(source_directory_fd)
    except OSError as exc:
        raise RuntimeError(
            f"Could not persist cleanup quarantine move for {relative_path}"
        ) from exc
    return _FileOperation(
        "deleted",
        size=size,
        quarantined=quarantined,
    )


def _open_target_parent(root_fd: int, relative_path: Path) -> tuple[int, str]:
    parts = relative_path.parts
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0] == _QUARANTINE_DIR_NAME
    ):
        raise RuntimeError(f"Invalid quarantined target path: {relative_path}")
    current_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, parts[-1]
    except Exception:
        os.close(current_fd)
        raise


def _regular_entry_exists(directory_fd: int, name: str) -> bool:
    try:
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(entry_stat.st_mode):
        raise RuntimeError(f"Quarantine entry is not a regular file: {name}")
    return True


def _restore_quarantined_file(
    root_fd: int,
    quarantine_fd: int,
    quarantined: _QuarantinedFile,
) -> None:
    parent_fd, filename = _open_target_parent(root_fd, quarantined.relative_path)
    try:
        staged_exists = _regular_entry_exists(
            quarantine_fd, quarantined.staged_name
        )
        try:
            os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            original_exists = True
        except FileNotFoundError:
            original_exists = False

        if staged_exists and not original_exists:
            os.rename(
                quarantined.staged_name,
                filename,
                src_dir_fd=quarantine_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
            os.fsync(quarantine_fd)
        elif not staged_exists and original_exists:
            pass
        elif staged_exists:
            raise RuntimeError(
                f"Cannot restore {quarantined.relative_path}: destination exists"
            )
        else:
            raise RuntimeError(
                f"Cannot restore {quarantined.relative_path}: staged file is missing"
            )
        _unlink_if_exists(quarantine_fd, quarantined.journal_name)
        os.fsync(quarantine_fd)
    finally:
        os.close(parent_fd)


def _purge_quarantined_file(
    quarantine_fd: int, quarantined: _QuarantinedFile
) -> None:
    if _regular_entry_exists(quarantine_fd, quarantined.staged_name):
        os.unlink(quarantined.staged_name, dir_fd=quarantine_fd)
        os.fsync(quarantine_fd)
    _unlink_if_exists(quarantine_fd, quarantined.journal_name)
    os.fsync(quarantine_fd)


def _with_quarantine(
    root: Path,
    operation,
) -> None:
    root_fd = _open_image_root(root)
    quarantine_fd = None
    try:
        quarantine_fd = _open_quarantine(root_fd, create=False)
        if quarantine_fd is None:
            raise RuntimeError("Cleanup quarantine disappeared")
        operation(root_fd, quarantine_fd)
    finally:
        if quarantine_fd is not None:
            os.close(quarantine_fd)
            _remove_quarantine_dir_if_empty(root_fd)
        os.close(root_fd)


def _restore_uncommitted_quarantine(root: Path) -> None:
    if not os.path.lexists(root / _QUARANTINE_DIR_NAME):
        return

    def restore_all(root_fd: int, quarantine_fd: int) -> None:
        errors: list[str] = []
        names = sorted(os.listdir(quarantine_fd))
        for name in names:
            if name.startswith(".") and name.endswith(".json.tmp"):
                _unlink_if_exists(quarantine_fd, name)
        for journal_name in (name for name in names if name.endswith(".json")):
            try:
                quarantined = _load_quarantine_journal(
                    quarantine_fd, journal_name
                )
                _restore_quarantined_file(
                    root_fd, quarantine_fd, quarantined
                )
            except Exception as exc:
                errors.append(f"{journal_name}: {exc}")
        leftovers = sorted(os.listdir(quarantine_fd))
        if errors or leftovers:
            raise RuntimeError(
                "Could not restore cleanup quarantine: "
                f"errors={errors} leftovers={leftovers}"
            )

    _with_quarantine(root, restore_all)


def _purge_quarantined_files(
    root: Path, quarantined_files: Sequence[_QuarantinedFile]
) -> None:
    if not quarantined_files:
        return

    def purge(_root_fd: int, quarantine_fd: int) -> None:
        for quarantined in quarantined_files:
            _purge_quarantined_file(quarantine_fd, quarantined)

    _with_quarantine(root, purge)


def _load_quarantine_journal(
    quarantine_fd: int, journal_name: str
) -> _QuarantinedFile:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    journal_fd = os.open(journal_name, flags, dir_fd=quarantine_fd)
    try:
        payload = os.read(journal_fd, _MAX_JOURNAL_BYTES + 1)
    finally:
        os.close(journal_fd)
    if len(payload) > _MAX_JOURNAL_BYTES:
        raise RuntimeError(f"Cleanup journal is too large: {journal_name}")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid cleanup journal: {journal_name}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid cleanup journal: {journal_name}")

    operation_id = str(data.get("operation_id", ""))
    expected_journal = f"{operation_id}.json"
    staged_name = str(data.get("staged_name", ""))
    relative_path = Path(str(data.get("relative_path", "")))
    raw_ids = data.get("image_file_ids")
    if (
        data.get("version") != _QUARANTINE_JOURNAL_VERSION
        or len(operation_id) != 32
        or any(character not in "0123456789abcdef" for character in operation_id)
        or journal_name != expected_journal
        or data.get("journal_name") != expected_journal
        or staged_name != f"{operation_id}.data"
        or relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
        or relative_path.parts[0] == _QUARANTINE_DIR_NAME
        or not isinstance(raw_ids, list)
    ):
        raise RuntimeError(f"Invalid cleanup journal fields: {journal_name}")
    try:
        image_file_ids = tuple(sorted({int(value) for value in raw_ids}))
        size = int(data.get("size"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid cleanup journal values: {journal_name}") from exc
    if (
        not image_file_ids
        or any(value <= 0 for value in image_file_ids)
        or size < 0
    ):
        raise RuntimeError(f"Invalid cleanup journal values: {journal_name}")
    return _QuarantinedFile(
        operation_id,
        journal_name,
        staged_name,
        relative_path,
        image_file_ids,
        size,
    )


def _quarantine_transaction_committed(
    cur,
    quarantined: _QuarantinedFile,
    *,
    image_root: Path,
    repo_root: Path,
) -> bool:
    expected = set(quarantined.image_file_ids)
    cur.execute(
        """
        SELECT image_file_id, file_path, cleaned_up_at IS NOT NULL
        FROM image_file
        WHERE image_file_id = ANY(%s)
        ORDER BY image_file_id
        """,
        (sorted(expected),),
    )
    rows = cur.fetchall()
    returned = {int(row[0]) for row in rows}
    if returned != expected:
        raise RuntimeError(
            "Cleanup recovery could not find all image_file rows: "
            f"missing={sorted(expected - returned)}"
        )
    committed = [row[1] is None and bool(row[2]) for row in rows]
    uncommitted = [row[1] is not None and not bool(row[2]) for row in rows]
    if all(committed):
        return True
    if all(uncommitted):
        expected_target = (image_root / quarantined.relative_path).resolve(
            strict=False
        )
        for image_file_id, file_path, _cleaned in rows:
            if not _db_path_can_resolve_to_target(
                str(file_path),
                target=expected_target,
                image_root=image_root,
                repo_root=repo_root,
            ):
                raise RuntimeError(
                    "Cleanup recovery found a repointed image_file path for "
                    f"image_file_id={image_file_id}"
                )
        return False
    raise RuntimeError(
        "Cleanup recovery found mixed database state for image_file_ids="
        f"{sorted(expected)}"
    )


def _db_path_can_resolve_to_target(
    raw_path: str,
    *,
    target: Path,
    image_root: Path,
    repo_root: Path,
) -> bool:
    if not raw_path or "\x00" in raw_path:
        return False
    path = Path(raw_path)
    return any(
        Path(os.path.abspath(candidate)).resolve(strict=False) == target
        for candidate in _db_path_candidates(
            path,
            image_root=image_root,
            repo_root=repo_root,
        )
    )


def _recover_quarantine(cur, root: Path) -> None:
    if not os.path.lexists(root / _QUARANTINE_DIR_NAME):
        return
    root_fd = _open_image_root(root)
    quarantine_fd = None
    try:
        quarantine_fd = _open_quarantine(root_fd, create=False)
        if quarantine_fd is None:
            return
        names = sorted(os.listdir(quarantine_fd))
        for name in names:
            if name.startswith(".") and name.endswith(".json.tmp"):
                _unlink_if_exists(quarantine_fd, name)
                os.fsync(quarantine_fd)
        journals = [name for name in names if name.endswith(".json")]
        errors: list[str] = []
        for journal_name in journals:
            try:
                quarantined = _load_quarantine_journal(
                    quarantine_fd, journal_name
                )
                if _quarantine_transaction_committed(
                    cur,
                    quarantined,
                    image_root=root,
                    repo_root=env_repo_root().resolve(),
                ):
                    _purge_quarantined_file(quarantine_fd, quarantined)
                else:
                    _restore_quarantined_file(
                        root_fd, quarantine_fd, quarantined
                    )
            except Exception as exc:
                errors.append(f"{journal_name}: {exc}")
                logger.exception(
                    "Could not reconcile cleanup journal %s", journal_name
                )
        leftovers = sorted(os.listdir(quarantine_fd))
        if errors or leftovers:
            raise RuntimeError(
                "Cleanup quarantine requires manual recovery: "
                f"errors={errors} leftovers={leftovers}"
            )
    finally:
        if quarantine_fd is not None:
            os.close(quarantine_fd)
            _remove_quarantine_dir_if_empty(root_fd)
        os.close(root_fd)


def _operate_on_target(
    root_fd: int,
    relative_path: Path,
    *,
    apply: bool,
    quarantine_fd: int | None,
    image_file_ids: tuple[int, ...],
) -> _FileOperation:
    parts = relative_path.parts
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0] == _QUARANTINE_DIR_NAME
    ):
        return _FileOperation("unsafe", error="invalid relative target")

    current_fd = os.dup(root_fd)
    try:
        directory_flags = _directory_open_flags()
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
        target_flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        target_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            target_fd = os.open(filename, target_flags, dir_fd=current_fd)
        except FileNotFoundError:
            return _FileOperation("missing")
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENXIO, errno.ENODEV}:
                return _FileOperation("unsafe", error="target is not a regular file")
            return _FileOperation("failed", error=str(exc))
        try:
            target_stat = os.fstat(target_fd)
            if not stat.S_ISREG(target_stat.st_mode):
                return _FileOperation("unsafe", error="target is not a regular file")
            if not apply:
                return _FileOperation("regular", size=target_stat.st_size)
            if quarantine_fd is None:
                return _FileOperation(
                    "failed", error="cleanup quarantine is unavailable"
                )
            return _stage_target(
                current_fd,
                filename,
                target_fd,
                target_stat,
                quarantine_fd,
                relative_path=relative_path,
                image_file_ids=image_file_ids,
                size=target_stat.st_size,
            )
        finally:
            os.close(target_fd)
    finally:
        os.close(current_fd)
