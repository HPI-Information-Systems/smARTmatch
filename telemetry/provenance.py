"""Runtime dependency, artifact, and build-provenance metadata."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from matching_pipeline.shared.env import env_str
from telemetry.build_provenance import GIT_SOURCE as BUILD_PROVENANCE_GIT_SOURCE
from telemetry.build_provenance import SCHEMA_VERSION as BUILD_PROVENANCE_SCHEMA_VERSION
from telemetry.build_provenance import (
    SOURCE_HASH_ALGORITHM,
    SOURCE_PATHS,
    source_snapshot,
)
from telemetry.constants import (
    _COMPONENT_REQUIREMENT_FILES,
    _GIT_OBJECT_ID_RE,
    _MAX_BUILD_PROVENANCE_BYTES,
    _REPRODUCIBILITY_PACKAGES,
    _REQUIREMENT_PIN_RE,
)
from telemetry.tree_hashing import _hash_file, _tree_record, hash_directory_tree


def _runtime_reproducibility_metadata() -> dict[str, Any]:
    app_root = Path(__file__).resolve().parents[1]
    package_root = app_root / "matching_pipeline"
    classifier = package_root / "image_matching" / "classifier.pkl"
    reference_root = package_root / "shared" / "reference_data"
    result: dict[str, Any] = {
        "models": {
            "dinov3": env_str("DINOV3_MODEL_ID"),
            "metadata_backend": env_str("METADATA_BACKEND"),
            "metadata_model": env_str("METADATA_MODEL"),
            "metadata_quantization": env_str("METADATA_QUANTIZATION"),
        },
        "configuration": {
            "matching_batch_size": env_str("MATCHING_BATCH_SIZE"),
            "max_similarity_string_length": env_str("MAX_SIM_STRING_LEN", "100"),
        },
        "artifacts": {},
        "packages": {},
        "requirement_locks": {},
    }
    result["artifacts"]["runtime_python_source_sha256"] = _runtime_source_hash(
        package_root
    )
    if classifier.is_file():
        result["artifacts"]["image_classifier_sha256"] = _hash_file(classifier)[0]
    if reference_root.is_dir():
        result["artifacts"]["reference_data_sha256"] = hash_directory_tree(
            reference_root
        ).root.sha256
    for component, relative_path in _COMPONENT_REQUIREMENT_FILES.items():
        result["requirement_locks"][component] = _requirement_lock_metadata(
            app_root / relative_path,
            relative_path=relative_path,
        )
    matching_packages = result["requirement_locks"]["matching_pipeline"]["packages"]
    for package in _REPRODUCIBILITY_PACKAGES:
        result["packages"][package] = matching_packages.get(package)
    return result


def _requirement_lock_metadata(
    path: Path,
    *,
    relative_path: Path | None = None,
) -> dict[str, Any]:
    display_path = (relative_path or path).as_posix()
    if not path.is_file():
        return {
            "path": display_path,
            "available": False,
            "sha256": None,
            "package_count": 0,
            "packages": {},
        }

    content = path.read_bytes()
    packages: dict[str, str] = {}
    for raw_line in content.decode("utf-8").splitlines():
        match = _REQUIREMENT_PIN_RE.match(raw_line.strip())
        if match is None:
            continue
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        version = match.group(2)
        previous = packages.setdefault(name, version)
        if previous != version:
            raise ValueError(
                f"Requirement lock {display_path} pins {name} to conflicting "
                f"versions {previous} and {version}"
            )
    return {
        "path": display_path,
        "available": True,
        "sha256": hashlib.sha256(content).hexdigest(),
        "package_count": len(packages),
        "packages": dict(sorted(packages.items())),
    }


def _git_identity() -> dict[str, Any]:
    provenance_path = env_str("SMARTMATCH_BUILD_PROVENANCE_FILE")
    provenance_required = _strict_optional_bool(
        "SMARTMATCH_REQUIRE_BUILD_PROVENANCE", default=False
    )
    if provenance_path is not None:
        return _build_provenance_identity(Path(provenance_path))
    if provenance_required:
        raise ValueError("The telemetry image requires baked build provenance")

    commit = env_str("SMARTMATCH_GIT_COMMIT")
    if commit is not None and _GIT_OBJECT_ID_RE.fullmatch(commit.lower()) is None:
        raise ValueError("SMARTMATCH_GIT_COMMIT must be a Git object ID")
    return {
        "commit": commit.lower() if commit else None,
        "source": "environment" if commit else "unavailable",
        "tracked_files_dirty": None,
        "build_source_sha256": None,
    }


def _strict_optional_bool(name: str, *, default: bool) -> bool:
    raw_value = env_str(name)
    if raw_value is None:
        return default
    normalized = raw_value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Environment variable {name} must be a boolean value")


def _build_provenance_identity(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_BUILD_PROVENANCE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"Build provenance is unavailable: {path}") from exc
    if len(raw) > _MAX_BUILD_PROVENANCE_BYTES:
        raise ValueError("Build provenance exceeds the fixed size limit")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Build provenance is not valid UTF-8 JSON") from exc
    if (
        not isinstance(document, Mapping)
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != BUILD_PROVENANCE_SCHEMA_VERSION
        or set(document) != {"schema_version", "git", "source"}
    ):
        raise ValueError("Build provenance has an unsupported schema")
    git = document.get("git")
    source = document.get("source")
    if (
        not isinstance(git, Mapping)
        or set(git) != {"commit", "source"}
        or git.get("source") != BUILD_PROVENANCE_GIT_SOURCE
        or not isinstance(source, Mapping)
        or set(source) != {"algorithm", "sha256", "file_count", "total_bytes", "paths"}
    ):
        raise ValueError("Build provenance is missing Git or source metadata")
    commit_value = git.get("commit")
    source_sha256_value = source.get("sha256")
    if (
        not isinstance(commit_value, str)
        or commit_value != commit_value.lower()
        or not isinstance(source_sha256_value, str)
        or source_sha256_value != source_sha256_value.lower()
    ):
        raise ValueError("Build provenance contains non-canonical hashes")
    commit = commit_value
    source_sha256 = source_sha256_value
    if source.get("algorithm") != SOURCE_HASH_ALGORITHM:
        raise ValueError("Build provenance contains an unsupported source hash")
    if _GIT_OBJECT_ID_RE.fullmatch(commit) is None:
        raise ValueError("Build provenance contains an invalid Git object ID")
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise ValueError("Build provenance contains an invalid source hash")
    file_count = source.get("file_count")
    total_bytes = source.get("total_bytes")
    if (
        isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or file_count < 1
        or isinstance(total_bytes, bool)
        or not isinstance(total_bytes, int)
        or total_bytes < 1
    ):
        raise ValueError("Build provenance contains invalid source counts")
    if source.get("paths") != list(SOURCE_PATHS):
        raise ValueError("Build provenance contains unexpected source paths")
    actual_source = source_snapshot(Path(__file__).resolve().parents[1])
    if dict(source) != actual_source:
        raise ValueError("Build provenance does not match the copied image source")
    return {
        "commit": commit,
        "source": "build_provenance",
        "tracked_files_dirty": None,
        "build_source_sha256": source_sha256,
        "build_source_file_count": file_count,
        "build_source_total_bytes": total_bytes,
        "worktree_state": "captured_by_build_source_sha256",
    }


def _runtime_source_hash(package_root: Path) -> str:
    app_root = package_root.parent
    source_roots = (package_root, app_root / "telemetry")
    paths = sorted(
        path
        for source_root in source_roots
        if source_root.is_dir()
        for path in source_root.rglob("*.py")
        if "__pycache__" not in path.parts
    )
    scheduler = app_root / "scripts" / "run_pipeline_scheduler.py"
    if scheduler.is_file():
        paths.append(scheduler)
    digest = hashlib.sha256(b"smartmatch-runtime-python-v1\0")
    for path in sorted(paths):
        relative = path.relative_to(app_root).as_posix().encode("utf-8")
        file_hash, size = _hash_file(path)
        digest.update(_tree_record(b"F", relative, size, file_hash))
    return digest.hexdigest()
