"""Generate recoverable Parquet candidate shards for image blocking."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from time import perf_counter
from typing import Callable, Mapping, Sequence

import numpy as np

from matching_pipeline.shared.artifacts import write_auction_to_lost_rankings_parquet
from matching_pipeline.shared.env import env_auction_to_lost_rankings_dir

from .input_sources import ImageFileRow
from .search import topk_cosine_similarity

logger = logging.getLogger(__name__)

CANDIDATE_IDENTITY_SCHEMA_VERSION = 2
_FINGERPRINT_CHUNK_SIZE = 1024 * 1024


def clear_candidate_parts(output_dir: Path | None = None) -> int:
    """Remove persisted candidate-ranking parts for an empty current run."""
    directory = output_dir or env_auction_to_lost_rankings_dir()
    directory.mkdir(parents=True, exist_ok=True)
    removed = 0
    for pattern in ("part-*.parquet", ".part-*.tmp.*"):
        for path in directory.glob(pattern):
            path.unlink()
            removed += 1
    if removed:
        logger.info("Cleared %d stale candidate artifact files from %s", removed, directory)
    return removed


def write_candidate_parts(
    auction_rows: Sequence[ImageFileRow],
    lost_file_ids: Sequence[str],
    lost_embeddings: np.ndarray,
    model_factory: Callable[[], object],
    *,
    model_identity: str,
    lost_source_identity: str,
    lost_content_versions: Mapping[str, int | None],
    lost_content_sha256: Mapping[str, str],
    top_k: int,
    image_batch_size: int,
    shard_size: int,
    force_rebuild_file_ids: set[str] | None = None,
) -> tuple[int, int, int]:
    output_dir = env_auction_to_lost_rankings_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    _remove_temp_parts(output_dir)
    forced_ids = {str(value) for value in (force_rebuild_file_ids or set())} | {
        row.file_id for row in auction_rows if row.is_embedded is False
    }
    try:
        normalized_model_identity = _required_identity(
            model_identity, "model_identity"
        )
        normalized_lost_source_identity = _required_identity(
            lost_source_identity, "lost_source_identity"
        )
        normalized_lost_versions = _lost_content_versions(
            lost_file_ids,
            lost_content_versions,
        )
        normalized_lost_digests = _lost_content_digests(
            lost_file_ids,
            lost_content_sha256,
        )
        lost_embedding_identity = _embedding_identity(
            lost_file_ids,
            lost_embeddings,
            lost_source_identity=normalized_lost_source_identity,
            lost_content_versions=normalized_lost_versions,
        )
        auction_image_identities = [_image_identity(row) for row in auction_rows]
    except Exception:
        clear_candidate_parts(output_dir)
        raise
    total = 0
    skipped = 0
    part_count = _part_count(len(auction_rows), shard_size)
    logger.info(
        "Candidate shard plan: auction_images=%d lost_images=%d top_k=%d shard_size=%d parts=%d output_dir=%s",
        len(auction_rows),
        len(lost_file_ids),
        top_k,
        shard_size,
        part_count,
        output_dir,
    )

    for part_index in range(part_count):
        shard_start = part_index * shard_size
        shard_end = (part_index + 1) * shard_size
        shard = auction_rows[shard_start:shard_end]
        part_path = _part_path(
            output_dir,
            part_index,
            auction_image_identities[shard_start:shard_end],
            lost_embedding_identity,
            normalized_model_identity,
            top_k,
        )
        _remove_stale_parts(output_dir, part_index, keep=part_path)
        force_rebuild = any(row.file_id in forced_ids for row in shard)
        if force_rebuild and part_path.is_file():
            part_path.unlink()
            logger.info(
                "Removed candidate part with DB-invalidated image IDs: %s",
                part_path,
            )
        total += len(shard) * min(top_k, len(lost_file_ids))
        logger.info(
            "Candidate part %d/%d planned: auction_images=%d path=%s",
            part_index + 1,
            part_count,
            len(shard),
            part_path,
        )
        if part_path.is_file():
            skipped += 1
            logger.info("Candidate part %d/%d already exists; skipping", part_index + 1, part_count)
            continue
        _write_part(
            part_path,
            shard,
            lost_file_ids,
            lost_embeddings,
            model_factory(),
            top_k,
            image_batch_size,
            part_index + 1,
            part_count,
            auction_image_identities[shard_start:shard_end],
            normalized_lost_versions,
            normalized_lost_digests,
        )

    current_image_identities = [_image_identity(row) for row in auction_rows]
    if current_image_identities != auction_image_identities:
        clear_candidate_parts(output_dir)
        raise RuntimeError("Auction images changed during candidate generation")
    _remove_parts_from_index(output_dir, part_count)
    return total, part_count, skipped


def _write_part(
    part_path,
    shard,
    lost_file_ids,
    lost_embeddings,
    model,
    top_k,
    batch_size,
    part_number,
    part_count,
    expected_image_identities,
    lost_content_versions,
    lost_content_sha256,
) -> None:
    part_started_at = perf_counter()
    columns = _empty_candidate_columns()
    for batch_start in range(0, len(shard), batch_size):
        batch = shard[batch_start : batch_start + batch_size]
        paths = [str(row.file_path) for row in batch]
        embeddings = np.asarray(model.generate_embeddings_batch(paths), dtype=np.float32)
        indices, scores = topk_cosine_similarity(embeddings, lost_embeddings, top_k=top_k)
        _append_candidates(
            columns,
            batch,
            lost_file_ids,
            indices,
            scores,
            expected_image_identities[batch_start : batch_start + len(batch)],
            lost_content_versions,
            lost_content_sha256,
        )
    if [_image_identity(row) for row in shard] != list(expected_image_identities):
        clear_candidate_parts(part_path.parent)
        raise RuntimeError("Auction images changed during candidate generation")
    write_auction_to_lost_rankings_parquet(part_path.name, **columns)
    logger.info(
        "Candidate part %d/%d written in %.1fs: rows=%d path=%s",
        part_number,
        part_count,
        perf_counter() - part_started_at,
        len(columns["auction_file_ids"]),
        part_path,
    )


def _append_candidates(
    columns,
    batch,
    lost_file_ids,
    indices,
    scores,
    auction_image_identities,
    lost_content_versions,
    lost_content_sha256,
) -> None:
    for row_idx, row in enumerate(batch):
        auction_identity = auction_image_identities[row_idx]
        for rank, lost_idx in enumerate(indices[row_idx], start=1):
            lost_index = int(lost_idx)
            columns["auction_file_ids"].append(row.file_id)
            columns["auction_content_versions"].append(row.content_version)
            columns["auction_content_sha256"].append(
                auction_identity["source_sha256"]
            )
            columns["lost_file_ids"].append(lost_file_ids[lost_index])
            columns["lost_content_versions"].append(
                lost_content_versions[lost_index]
            )
            columns["lost_content_sha256"].append(
                lost_content_sha256[lost_index]
            )
            columns["ranks"].append(rank)
            columns["blocking_scores"].append(float(scores[row_idx, rank - 1]))


def _required_identity(value: object, name: str) -> str:
    identity = str(value).strip() if value is not None else ""
    if not identity:
        raise ValueError(f"{name} must not be empty")
    return identity


def _embedding_identity(
    lost_file_ids: Sequence[str],
    lost_embeddings: np.ndarray,
    *,
    lost_source_identity: str,
    lost_content_versions: Sequence[int | None] | None = None,
) -> str:
    embeddings = np.asarray(lost_embeddings)
    if embeddings.ndim != 2:
        raise ValueError(
            f"Expected a 2D lost embedding array, got {embeddings.shape}"
        )
    if embeddings.shape[0] != len(lost_file_ids):
        raise ValueError(
            "Lost embedding row count does not match lost_file_ids: "
            f"{embeddings.shape[0]} != {len(lost_file_ids)}"
        )
    if not np.all(np.isfinite(embeddings)):
        raise ValueError("Lost embeddings contain NaN or infinite values")

    contiguous = np.ascontiguousarray(embeddings)
    digest = hashlib.sha256()
    _digest_part(
        digest,
        _required_identity(
            lost_source_identity, "lost_source_identity"
        ).encode("utf-8"),
    )
    _digest_part(digest, str(contiguous.dtype).encode("ascii"))
    _digest_part(
        digest,
        json.dumps(list(contiguous.shape), separators=(",", ":")).encode("ascii"),
    )
    versions = (
        list(lost_content_versions)
        if lost_content_versions is not None
        else [None] * len(lost_file_ids)
    )
    if len(versions) != len(lost_file_ids):
        raise ValueError(
            "Lost content-version count does not match lost_file_ids: "
            f"{len(versions)} != {len(lost_file_ids)}"
        )
    for file_id, version in zip(lost_file_ids, versions, strict=True):
        _digest_part(digest, str(file_id).encode("utf-8"))
        _digest_part(
            digest,
            ("" if version is None else str(version)).encode("ascii"),
        )
    _digest_part(digest, contiguous.tobytes())
    return digest.hexdigest()


def _lost_content_versions(
    lost_file_ids: Sequence[str],
    values: Mapping[str, int | None],
) -> list[int | None]:
    missing = [file_id for file_id in lost_file_ids if file_id not in values]
    if missing:
        raise ValueError(
            "Missing lost content versions for file_id values: "
            + ", ".join(missing)
        )
    result: list[int | None] = []
    for file_id in lost_file_ids:
        value = values[file_id]
        if value is None:
            result.append(None)
            continue
        version = int(value)
        if version <= 0:
            raise ValueError(
                f"Lost content version must be positive for file_id={file_id}: {version}"
            )
        result.append(version)
    return result


def _lost_content_digests(
    lost_file_ids: Sequence[str],
    values: Mapping[str, str],
) -> list[str]:
    missing = [file_id for file_id in lost_file_ids if file_id not in values]
    if missing:
        raise ValueError(
            "Missing lost content digests for file_id values: "
            + ", ".join(missing)
        )
    result: list[str] = []
    for file_id in lost_file_ids:
        digest = str(values[file_id]).strip().lower()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(
                f"Invalid lost content digest for file_id={file_id}: {digest!r}"
            )
        result.append(digest)
    return result


def _image_identity(row: ImageFileRow) -> dict[str, object]:
    raw_path = Path(row.file_path).expanduser()
    absolute_path = raw_path.absolute()
    resolved_path = raw_path.resolve(strict=True)
    initial_stat = resolved_path.stat()
    source_digest = hashlib.sha256()
    with resolved_path.open("rb") as source:
        while chunk := source.read(_FINGERPRINT_CHUNK_SIZE):
            source_digest.update(chunk)
    final_stat = resolved_path.stat()
    if _stat_signature(initial_stat) != _stat_signature(final_stat):
        raise RuntimeError(f"Auction image changed while fingerprinting: {raw_path}")
    source_sha256 = source_digest.hexdigest()
    if row.content_sha256 is not None and row.content_sha256 != source_sha256:
        raise RuntimeError(
            "Auction image content does not match its database digest: "
            f"image_file_id={row.file_id} path={raw_path}"
        )
    return {
        "file_id": str(row.file_id),
        "file_path": str(absolute_path),
        "resolved_file_path": str(resolved_path),
        "source_size": initial_stat.st_size,
        "source_sha256": source_sha256,
        "content_version": row.content_version,
        "database_content_sha256": row.content_sha256,
    }


def _stat_signature(value) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _part_path(
    output_dir: Path,
    index: int,
    auction_image_identities: Sequence[dict[str, object]],
    lost_embedding_identity: str,
    model_identity: str,
    top_k: int,
) -> Path:
    identity = {
        "schema_version": CANDIDATE_IDENTITY_SCHEMA_VERSION,
        "part_index": index,
        "top_k": top_k,
        "model_identity": _required_identity(model_identity, "model_identity"),
        "lost_embedding_sha256": lost_embedding_identity,
        "auction_images": list(auction_image_identities),
    }
    serialized = json.dumps(
        identity,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    return output_dir / f"part-{index:06d}-{digest}.parquet"


def _digest_part(digest, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _remove_stale_parts(output_dir: Path, index: int, *, keep: Path) -> None:
    for path in output_dir.glob(f"part-{index:06d}*.parquet"):
        if path != keep:
            path.unlink()


def _remove_temp_parts(output_dir: Path) -> None:
    for path in output_dir.glob(".part-*.tmp.*"):
        path.unlink()


def _remove_parts_from_index(output_dir: Path, start_index: int) -> None:
    for path in output_dir.glob("part-*.parquet"):
        index = _part_index(path)
        if index is None or index >= start_index:
            path.unlink()


def _part_index(path: Path) -> int | None:
    pieces = path.name.split("-")
    if len(pieces) < 2 or pieces[0] != "part":
        return None
    try:
        return int(pieces[1].split(".")[0])
    except ValueError:
        return None


def _part_count(row_count: int, shard_size: int) -> int:
    return (row_count + shard_size - 1) // shard_size


def _empty_candidate_columns() -> dict[str, list]:
    return {
        "auction_file_ids": [],
        "auction_content_versions": [],
        "auction_content_sha256": [],
        "lost_file_ids": [],
        "lost_content_versions": [],
        "lost_content_sha256": [],
        "ranks": [],
        "blocking_scores": [],
    }
