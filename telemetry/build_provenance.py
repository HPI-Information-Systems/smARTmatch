"""Generate immutable telemetry image provenance from copied build inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
GIT_SOURCE = "docker_build_context_git_refs"
SOURCE_HASH_ALGORITHM = "sha256-path-mode-content-tree-v1"
SOURCE_PATHS = (
    "requirements.txt",
    "matching_pipeline",
    "scripts/run_pipeline_scheduler.py",
    "shared",
    "telemetry",
)
_HASH_PREFIX = b"smartmatch-build-source-v1\0"
_GIT_OBJECT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_MAX_GIT_REFERENCE_BYTES = 4 * 1024
_MAX_PACKED_REFS_BYTES = 16 * 1024 * 1024
_MAX_SYMBOLIC_REF_DEPTH = 16


def validate_commit(value: str) -> str:
    commit = value.strip().lower()
    if _GIT_OBJECT_RE.fullmatch(commit) is None:
        raise ValueError("Build Git commit must be a 40- or 64-character hex object ID")
    return commit


def _validate_reference_name(value: str) -> str:
    reference = value.strip()
    components = reference.split("/")
    if (
        not reference.startswith("refs/")
        or any(component in {"", ".", ".."} for component in components)
        or ".." in reference
        or "@{" in reference
        or reference.endswith((".", ".lock"))
        or any(
            ord(character) <= 0x20
            or ord(character) == 0x7F
            or character in "~^:?*[\\"
            for character in reference
        )
    ):
        raise ValueError(f"Build Git metadata contains an invalid ref: {reference!r}")
    return reference


def _read_bounded_text(path: Path, *, maximum: int, label: str) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(maximum + 1)
    except OSError as exc:
        raise ValueError(f"Build Git {label} is unreadable: {path}") from exc
    if len(raw) > maximum:
        raise ValueError(f"Build Git {label} exceeds the fixed size limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Build Git {label} is not valid UTF-8") from exc


def _single_reference_value(path: Path, *, label: str) -> str:
    lines = _read_bounded_text(
        path,
        maximum=_MAX_GIT_REFERENCE_BYTES,
        label=label,
    ).splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise ValueError(f"Build Git {label} must contain exactly one value")
    return lines[0].strip()


def _packed_reference(git_dir: Path, reference: str) -> str | None:
    packed_refs = git_dir / "packed-refs"
    if not packed_refs.exists():
        return None
    try:
        packed_refs.resolve().relative_to(git_dir)
    except ValueError as exc:
        raise ValueError("Build Git packed-refs escapes its metadata directory") from exc
    content = _read_bounded_text(
        packed_refs,
        maximum=_MAX_PACKED_REFS_BYTES,
        label="packed-refs",
    )
    commit: str | None = None
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "^")):
            continue
        fields = stripped.split(maxsplit=1)
        if len(fields) != 2 or fields[1] != reference:
            continue
        candidate = validate_commit(fields[0])
        if commit is not None and commit != candidate:
            raise ValueError(f"Build Git packed-refs repeats {reference!r}")
        commit = candidate
    return commit


def resolve_git_head(git_dir: Path) -> str:
    """Resolve HEAD using only bounded loose and packed Git reference files."""
    if git_dir.is_file():
        raise ValueError(
            "Build Git metadata is an external gitdir pointer; use a standalone "
            "checkout as the Docker build context"
        )
    if not git_dir.is_dir():
        raise ValueError(f"Build Git metadata directory is unavailable: {git_dir}")

    root = git_dir.resolve()
    reference = "HEAD"
    visited: set[str] = set()
    for _ in range(_MAX_SYMBOLIC_REF_DEPTH):
        if reference in visited:
            raise ValueError("Build Git metadata contains a symbolic-ref cycle")
        visited.add(reference)

        if reference == "HEAD":
            path = root / "HEAD"
        else:
            reference = _validate_reference_name(reference)
            path = root.joinpath(*reference.split("/"))
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError("Build Git reference escapes its metadata directory") from exc

        if path.is_file():
            value = _single_reference_value(path, label=reference)
        elif reference != "HEAD":
            value = _packed_reference(root, reference)
            if value is None:
                raise ValueError(f"Build Git reference is unavailable: {reference}")
        else:
            raise ValueError("Build Git HEAD is unavailable")

        if value.startswith("ref:"):
            reference = _validate_reference_name(value.removeprefix("ref:"))
            continue
        return validate_commit(value)
    raise ValueError("Build Git symbolic-ref depth exceeds the fixed limit")


def _source_files(app_root: Path) -> list[Path]:
    files: list[Path] = []
    for relative in SOURCE_PATHS:
        path = app_root / relative
        if path.is_file() or path.is_symlink():
            files.append(path)
            continue
        if not path.is_dir():
            raise FileNotFoundError(f"Build provenance input is missing: {relative}")
        files.extend(
            child
            for child in path.rglob("*")
            if (child.is_file() or child.is_symlink())
            and "__pycache__" not in child.parts
        )
    return sorted(files, key=lambda item: os.fsencode(item.relative_to(app_root)))


def source_snapshot(app_root: Path) -> dict[str, Any]:
    """Hash every copied source/lock file by path, mode, size, and content."""
    digest = hashlib.sha256(_HASH_PREFIX)
    file_count = 0
    total_bytes = 0
    for path in _source_files(app_root):
        relative = path.relative_to(app_root).as_posix()
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            kind = "symlink"
            content = os.readlink(path).encode("utf-8", errors="surrogateescape")
        else:
            kind = "file"
            content = path.read_bytes()
        content_hash = hashlib.sha256(content).hexdigest()
        record = json.dumps(
            {
                "kind": kind,
                "mode": mode,
                "path": relative,
                "sha256": content_hash,
                "size": len(content),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest.update(len(record).to_bytes(8, "big"))
        digest.update(record)
        file_count += 1
        total_bytes += len(content)
    return {
        "algorithm": SOURCE_HASH_ALGORITHM,
        "sha256": digest.hexdigest(),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "paths": list(SOURCE_PATHS),
    }


def build_provenance(app_root: Path, commit: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "git": {
            "commit": validate_commit(commit),
            "source": GIT_SOURCE,
        },
        "source": source_snapshot(app_root),
    }


def write_provenance(output: Path, document: dict[str, Any]) -> None:
    encoded = (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_args(arguments: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--git-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(arguments)


def main(arguments: Iterable[str] | None = None) -> int:
    args = _parse_args(arguments)
    write_provenance(
        args.output,
        build_provenance(
            args.app_root.resolve(),
            resolve_git_head(args.git_dir),
        ),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
