"""Persistent Hugging Face safetensors metadata used by early GPU checks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from huggingface_hub import HfApi

from matching_pipeline.shared.env import env_cache_dir

_CACHE_SCHEMA_VERSION = 1
_DEFAULT_REVISION = "main"
_MODEL_INFO_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class HfSafetensorsMetadata:
    model_id: str
    requested_revision: str
    resolved_revision: str
    parameter_counts_by_dtype: dict[str, int]
    total_parameters: int


def get_cached_hf_safetensors_metadata(
    model_id: str,
    *,
    token: str | None,
    revision: str = _DEFAULT_REVISION,
    cache_dir: Path | None = None,
) -> HfSafetensorsMetadata:
    """Return model metadata, requesting the Hub only on a locked cache miss."""
    normalized_model_id = model_id.strip()
    normalized_revision = revision.strip()
    if not normalized_model_id:
        raise ValueError("model_id must not be empty")
    if not normalized_revision:
        raise ValueError("revision must not be empty")

    root = (
        Path(cache_dir)
        if cache_dir is not None
        else env_cache_dir() / "model_metadata" / "huggingface"
    )
    root.mkdir(mode=0o750, parents=True, exist_ok=True)
    identity = hashlib.sha256(
        f"{normalized_model_id}\0{normalized_revision}".encode("utf-8")
    ).hexdigest()
    cache_path = root / f"{identity}.json"
    lock_path = root / f".{identity}.lock"

    lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    lock_flags |= getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock_path, lock_flags, 0o640)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        cached = _read_cache(
            cache_path,
            model_id=normalized_model_id,
            revision=normalized_revision,
        )
        if cached is not None:
            return cached

        fetched = _fetch_metadata(
            normalized_model_id,
            revision=normalized_revision,
            token=token,
        )
        _write_cache(cache_path, fetched)
        return fetched
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _fetch_metadata(
    model_id: str,
    *,
    revision: str,
    token: str | None,
) -> HfSafetensorsMetadata:
    info = HfApi().model_info(
        model_id,
        revision=revision,
        token=token,
        timeout=_MODEL_INFO_TIMEOUT_SECONDS,
        expand=["safetensors", "sha"],
    )
    safetensors = info.safetensors
    if safetensors is None or not safetensors.parameters:
        raise ValueError(f"No safetensors parameter metadata found for {model_id}")
    if not info.sha:
        raise ValueError(f"No resolved Hugging Face revision found for {model_id}")

    counts = {
        str(dtype).upper(): int(count)
        for dtype, count in safetensors.parameters.items()
    }
    if any(count < 0 for count in counts.values()):
        raise ValueError(f"Invalid safetensors parameter metadata for {model_id}")
    total = int(safetensors.total)
    if total <= 0 or sum(counts.values()) != total:
        raise ValueError(f"Inconsistent safetensors parameter metadata for {model_id}")
    return HfSafetensorsMetadata(
        model_id=model_id,
        requested_revision=revision,
        resolved_revision=str(info.sha),
        parameter_counts_by_dtype=counts,
        total_parameters=total,
    )


def _read_cache(
    path: Path,
    *,
    model_id: str,
    revision: str,
) -> HfSafetensorsMetadata | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != _CACHE_SCHEMA_VERSION:
            return None
        metadata = HfSafetensorsMetadata(
            model_id=str(payload["model_id"]),
            requested_revision=str(payload["requested_revision"]),
            resolved_revision=str(payload["resolved_revision"]),
            parameter_counts_by_dtype={
                str(dtype): int(count)
                for dtype, count in payload["parameter_counts_by_dtype"].items()
            },
            total_parameters=int(payload["total_parameters"]),
        )
    except (
        AttributeError,
        FileNotFoundError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None

    if metadata.model_id != model_id or metadata.requested_revision != revision:
        return None
    if not metadata.resolved_revision:
        return None
    if metadata.total_parameters <= 0:
        return None
    if any(count < 0 for count in metadata.parameter_counts_by_dtype.values()):
        return None
    if sum(metadata.parameter_counts_by_dtype.values()) != metadata.total_parameters:
        return None
    return metadata


def _write_cache(path: Path, metadata: HfSafetensorsMetadata) -> None:
    payload: dict[str, Any] = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        **asdict(metadata),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.",
        suffix=".json",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o640)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
