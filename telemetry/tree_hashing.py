"""Deterministic, resource-bounded filesystem tree hashing."""

import hashlib
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from telemetry.constants import (
    _CONTENT_TREE_HASH_PREFIX,
    _HASH_CHUNK_BYTES,
    _MAX_HASH_ERRORS,
    _MAX_REPORTED_SUBDIRECTORIES,
    _MAX_TREE_ORDERING_BYTES,
    _MIN_TREE_ORDERING_FREE_BYTES,
    _PATH_SIZE_FILE_HASH_PREFIX,
    _PATH_SIZE_TREE_HASH_PREFIX,
)
from telemetry.models import DirectoryHash, TreeHashSnapshot


def _check_tree_ordering_budget(database_path: str) -> None:
    path = Path(database_path)
    size = path.stat().st_size
    free = shutil.disk_usage(path.parent).free
    if size > _MAX_TREE_ORDERING_BYTES:
        raise OSError(
            f"Directory ordering index exceeds {_MAX_TREE_ORDERING_BYTES} bytes"
        )
    if free < _MIN_TREE_ORDERING_FREE_BYTES:
        raise OSError(
            "Directory ordering index breached the filesystem free-space reserve"
        )


@contextmanager
def _externally_sorted_directory_names(directory: Path) -> Iterator[Iterator[str]]:
    """Yield directory entry names in byte order using a disk-backed B-tree."""
    descriptor, database_path = tempfile.mkstemp(prefix="smartmatch-tree-order-")
    os.close(descriptor)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database_path)
        connection.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = FILE;
            PRAGMA cache_size = -2048;
            CREATE TABLE entry_name (name BLOB PRIMARY KEY) WITHOUT ROWID;
            """
        )
        batch: list[tuple[bytes]] = []
        with os.scandir(directory) as entries:
            for entry in entries:
                batch.append((os.fsencode(entry.name),))
                if len(batch) >= 4_096:
                    connection.executemany(
                        "INSERT INTO entry_name(name) VALUES (?)", batch
                    )
                    connection.commit()
                    _check_tree_ordering_budget(database_path)
                    batch.clear()
        if batch:
            connection.executemany("INSERT INTO entry_name(name) VALUES (?)", batch)
            connection.commit()
            _check_tree_ordering_budget(database_path)
        cursor = connection.execute("SELECT name FROM entry_name ORDER BY name")
        yield (os.fsdecode(row[0]) for row in cursor)
    except sqlite3.Error as exc:
        raise OSError(f"Could not externally order directory entries: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
        Path(database_path).unlink(missing_ok=True)


def hash_directory_tree(
    root: Path,
    *,
    retain_file_paths: set[str] | None = None,
    hash_file_contents: bool = True,
) -> TreeHashSnapshot:
    """Hash a tree deterministically without following symlinks.

    Content mode is retained for small reproducibility artifacts. Image callers
    use path/size mode so telemetry never opens or reads image file contents.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Telemetry image directory does not exist: {root}")
    file_hashes: dict[str, str] = {}
    errors: list[str] = []
    error_count = 0
    subdirectories: dict[str, DirectoryHash] = {}
    subdirectory_count = 0

    def record_error(relative_path: str, exc: OSError) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < _MAX_HASH_ERRORS:
            errors.append(f"{relative_path}:{type(exc).__name__}")

    hash_basis = "file_content" if hash_file_contents else "relative_path_and_size"
    tree_prefix = (
        _CONTENT_TREE_HASH_PREFIX if hash_file_contents else _PATH_SIZE_TREE_HASH_PREFIX
    )

    def visit(directory: Path, relative: Path) -> DirectoryHash:
        nonlocal subdirectory_count
        digest = hashlib.sha256(tree_prefix)
        file_count = 0
        total_bytes = 0
        try:
            with _externally_sorted_directory_names(directory) as entry_names:
                for entry_name in entry_names:
                    entry = directory / entry_name
                    entry_relative = relative / entry_name
                    name = os.fsencode(entry_name)
                    try:
                        if entry.is_symlink():
                            target_hash = hashlib.sha256(
                                os.fsencode(os.readlink(entry))
                            ).hexdigest()
                            digest.update(_tree_record(b"L", name, 0, target_hash))
                        elif entry.is_dir():
                            child = visit(entry, entry_relative)
                            digest.update(
                                _tree_record(
                                    b"D", name, child.total_bytes, child.sha256
                                )
                            )
                            file_count += child.file_count
                            total_bytes += child.total_bytes
                            if relative == Path():
                                subdirectory_count += 1
                                if len(subdirectories) < _MAX_REPORTED_SUBDIRECTORIES:
                                    subdirectories[entry_name] = child
                        elif entry.is_file():
                            relative_text = entry_relative.as_posix()
                            if hash_file_contents:
                                file_hash, size = _hash_file(entry)
                            else:
                                size = entry.stat(follow_symlinks=False).st_size
                                file_hash = _path_size_file_hash(relative_text, size)
                            digest.update(_tree_record(b"F", name, size, file_hash))
                            if (
                                retain_file_paths is None
                                or relative_text in retain_file_paths
                            ):
                                file_hashes[relative_text] = file_hash
                            file_count += 1
                            total_bytes += size
                        else:
                            digest.update(_tree_record(b"O", name, 0, ""))
                    except OSError as exc:
                        record_error(entry_relative.as_posix(), exc)
                        error_hash = hashlib.sha256(
                            type(exc).__name__.encode("ascii")
                        ).hexdigest()
                        digest.update(_tree_record(b"E", name, 0, error_hash))
        except OSError as exc:
            record_error(relative.as_posix() or ".", exc)
            digest = hashlib.sha256(tree_prefix)
            digest.update(b"unreadable-directory\0")
            return DirectoryHash(digest.hexdigest(), 0, 0)
        return DirectoryHash(digest.hexdigest(), file_count, total_bytes)

    root_hash = visit(root, Path())
    return TreeHashSnapshot(
        root=root_hash,
        subdirectories=dict(subdirectories),
        subdirectory_count=subdirectory_count,
        subdirectories_truncated=(subdirectory_count > _MAX_REPORTED_SUBDIRECTORIES),
        file_hashes=file_hashes,
        hash_basis=hash_basis,
        error_count=error_count,
        errors=tuple(errors),
    )


def _path_size_file_hash(relative_path: str, size: int) -> str:
    digest = hashlib.sha256(_PATH_SIZE_FILE_HASH_PREFIX)
    encoded_path = relative_path.encode("utf-8", errors="surrogateescape")
    digest.update(str(len(encoded_path)).encode("ascii"))
    digest.update(b"\0")
    digest.update(encoded_path)
    digest.update(b"\0")
    digest.update(str(size).encode("ascii"))
    return digest.hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    before_identity = (before.st_size, before.st_mtime_ns, before.st_ino)
    after_identity = (after.st_size, after.st_mtime_ns, after.st_ino)
    if before_identity != after_identity:
        raise OSError("file changed while being hashed")
    return digest.hexdigest(), after.st_size


def _tree_record(kind: bytes, name: bytes, size: int, digest: str) -> bytes:
    return (
        b"\0".join(
            (
                kind,
                str(len(name)).encode("ascii"),
                name,
                str(size).encode("ascii"),
                digest.encode("ascii"),
            )
        )
        + b"\0"
    )
