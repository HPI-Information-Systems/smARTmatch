"""Persistent cache for lost-artwork blocking embeddings."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Callable, Sequence

import numpy as np

from matching_pipeline.shared.env import env_dinov3_model_id

from .config import DEFAULT_EMBEDDING_DTYPE
from .input_sources import ImageFileRow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LostEmbeddingCache:
    file_ids: list[str]
    embeddings: np.ndarray
    metadata: dict[str, object]


def load_dino_adapter_class():
    from .dino_adapter import DinoV3Adapter

    return DinoV3Adapter


def ensure_lost_embedding_cache(
    rows: Sequence[ImageFileRow],
    *,
    cache_path: Path,
    model_factory: Callable[[], object],
    batch_size: int,
    dtype: str = DEFAULT_EMBEDDING_DTYPE,
) -> LostEmbeddingCache:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    logger.info(
        "Checking lost embedding cache: path=%s requested=%d batch_size=%d dtype=%s",
        cache_path,
        len(rows),
        batch_size,
        dtype,
    )
    cached = load_lost_embedding_cache(cache_path)
    cached_map = _valid_cached_embeddings(cached)

    missing = [row for row in rows if row.file_id not in cached_map]
    logger.info(
        "Lost embedding cache status: usable_cached=%d missing=%d",
        len(cached_map),
        len(missing),
    )
    generated: dict[str, np.ndarray] = {}
    if missing:
        generated = _generate_embeddings(model_factory(), missing, batch_size)

    file_ids = [row.file_id for row in rows]
    ordered = [
        generated[row.file_id] if row.file_id in generated else cached_map[row.file_id]
        for row in rows
    ]
    embeddings = normalize_embeddings(np.stack(ordered).astype(np.float32))
    stored_embeddings = embeddings.astype(_numpy_dtype(dtype), copy=False)
    embedding_dim = int(embeddings.shape[1])

    if missing or _cache_needs_rewrite(cached, file_ids, dtype, embedding_dim):
        metadata = _metadata(dtype, len(file_ids), embedding_dim)
        logger.info(
            "Writing lost embedding cache: path=%s count=%d dim=%d dtype=%s",
            cache_path,
            len(file_ids),
            embedding_dim,
            dtype,
        )
        write_lost_embedding_cache(cache_path, file_ids, stored_embeddings, metadata)
    else:
        metadata = cached.metadata if cached is not None else _metadata(dtype, len(file_ids), embedding_dim)
        logger.info("Lost embedding cache is already up to date")

    return LostEmbeddingCache(file_ids, embeddings, metadata | {"generated_count": len(missing)})


def load_lost_embedding_cache(cache_path: Path) -> LostEmbeddingCache | None:
    if not cache_path.is_file():
        logger.info("No existing lost embedding cache found at %s", cache_path)
        return None
    try:
        with np.load(cache_path, allow_pickle=False) as data:
            file_ids = [str(value) for value in data["file_ids"].tolist()]
            embeddings = np.asarray(data["embeddings"])
            metadata = json.loads(str(data["metadata"].item()))
    except Exception as exc:
        raise ValueError(f"Invalid lost embedding cache: {cache_path}") from exc
    _validate_embeddings(embeddings)
    logger.info(
        "Loaded lost embedding cache: path=%s count=%d dim=%d model=%s dtype=%s",
        cache_path,
        len(file_ids),
        int(embeddings.shape[1]),
        metadata.get("model_id"),
        metadata.get("dtype"),
    )
    return LostEmbeddingCache(file_ids, embeddings, metadata)


def write_lost_embedding_cache(
    cache_path: Path,
    file_ids: Sequence[str],
    embeddings: np.ndarray,
    metadata: dict[str, object],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_name(f".{cache_path.name}.tmp.{os.getpid()}.npz")
    file_id_array = _string_array(file_ids)
    metadata_array = np.array(json.dumps(metadata, sort_keys=True))
    try:
        np.savez(tmp, file_ids=file_id_array, embeddings=embeddings, metadata=metadata_array)
        os.replace(tmp, cache_path)
    finally:
        if tmp.exists():
            tmp.unlink()


def normalize_embeddings(embeddings: np.ndarray, expected_dim: int | None = None) -> np.ndarray:
    arr = np.asarray(embeddings, dtype=np.float32)
    _validate_embeddings(arr, expected_dim)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise ValueError("Embedding batch contains zero vectors")
    return arr / norms


def _generate_embeddings(
    model: object,
    rows: Sequence[ImageFileRow],
    batch_size: int,
) -> dict[str, np.ndarray]:
    model_dim = _model_dimension(model)
    generated: dict[str, np.ndarray] = {}
    total_batches = _batch_count(len(rows), batch_size)
    logger.info(
        "Generating missing lost embeddings: images=%d batches=%d dim=%d",
        len(rows),
        total_batches,
        model_dim,
    )
    for batch_number, start in enumerate(range(0, len(rows), batch_size), start=1):
        batch_started_at = perf_counter()
        batch = rows[start : start + batch_size]
        paths = [str(row.file_path) for row in batch]
        logger.info(
            "Lost embedding batch %d/%d starting: images=%d first_file_id=%s first_path=%s",
            batch_number,
            total_batches,
            len(batch),
            batch[0].file_id,
            batch[0].file_path,
        )
        embeddings = normalize_embeddings(
            np.asarray(model.generate_embeddings_batch(paths)),
            expected_dim=model_dim,
        )
        for row, embedding in zip(batch, embeddings, strict=True):
            generated[row.file_id] = embedding
        logger.info(
            "Lost embedding batch %d/%d finished in %.1fs",
            batch_number,
            total_batches,
            perf_counter() - batch_started_at,
        )
    return generated


def _valid_cached_embeddings(cache: LostEmbeddingCache | None) -> dict[str, np.ndarray]:
    if cache is None:
        return {}
    if cache.metadata.get("model_id") != env_dinov3_model_id():
        logger.info(
            "Ignoring lost embedding cache for model %s; active model is %s",
            cache.metadata.get("model_id"),
            env_dinov3_model_id(),
        )
        return {}
    embedding_dim = _metadata_embedding_dim(cache.metadata)
    if embedding_dim != cache.embeddings.shape[1]:
        logger.info(
            "Ignoring lost embedding cache with metadata dim %s and array dim %d",
            embedding_dim,
            int(cache.embeddings.shape[1]),
        )
        return {}
    embeddings = normalize_embeddings(cache.embeddings, expected_dim=embedding_dim)
    return dict(zip(cache.file_ids, embeddings, strict=True))


def _cache_needs_rewrite(
    cache: LostEmbeddingCache | None,
    file_ids: Sequence[str],
    dtype: str,
    embedding_dim: int,
) -> bool:
    if cache is None:
        return True
    return (
        cache.file_ids != list(file_ids)
        or cache.metadata.get("dtype") != dtype
        or cache.metadata.get("model_id") != env_dinov3_model_id()
        or _metadata_embedding_dim(cache.metadata) != embedding_dim
    )


def _metadata(dtype: str, count: int, embedding_dim: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "model_id": env_dinov3_model_id(),
        "embedding_dim": embedding_dim,
        "dtype": dtype,
        "normalized": True,
        "lost_image_count": count,
    }


def _validate_embeddings(embeddings: np.ndarray, expected_dim: int | None = None) -> None:
    if embeddings.ndim != 2:
        raise ValueError(f"Expected a 2D embedding array, got {embeddings.shape}")
    if embeddings.shape[1] <= 0:
        raise ValueError(f"Expected positive embedding dimension, got {embeddings.shape}")
    if expected_dim is not None and embeddings.shape[1] != expected_dim:
        raise ValueError(f"Expected embedding dimension {expected_dim}, got {embeddings.shape}")
    if not np.all(np.isfinite(embeddings)):
        raise ValueError("Embedding array contains NaN or infinite values")


def _model_dimension(model: object) -> int:
    dim = int(model.get_dimension())
    if dim <= 0:
        raise RuntimeError(f"DINOv3 adapter returned invalid dimension: {dim}")
    return dim


def _metadata_embedding_dim(metadata: dict[str, object]) -> int:
    try:
        return int(metadata.get("embedding_dim", -1))
    except (TypeError, ValueError):
        return -1


def _batch_count(row_count: int, batch_size: int) -> int:
    return (row_count + batch_size - 1) // batch_size


def _numpy_dtype(dtype: str) -> np.dtype:
    if dtype not in {"float16", "float32"}:
        raise ValueError("dtype must be 'float16' or 'float32'")
    return np.dtype(dtype)


def _string_array(values: Sequence[str]) -> np.ndarray:
    max_len = max([1, *(len(value) for value in values)])
    return np.asarray(list(values), dtype=f"<U{max_len}")
