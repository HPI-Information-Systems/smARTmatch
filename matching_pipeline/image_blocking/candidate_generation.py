"""Generate recoverable Parquet candidate shards for image blocking."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from time import perf_counter
from typing import Callable, Sequence

import numpy as np

from matching_pipeline.shared.env import env_auction_to_lost_rankings_dir
from matching_pipeline.shared.artifacts import write_auction_to_lost_rankings_parquet

from .input_sources import ImageFileRow
from .search import topk_cosine_similarity

logger = logging.getLogger(__name__)


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
    top_k: int,
    image_batch_size: int,
    shard_size: int,
) -> tuple[int, int, int]:
    output_dir = env_auction_to_lost_rankings_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    _remove_temp_parts(output_dir)
    lost_digest = _digest_strings(lost_file_ids)
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
        shard = auction_rows[part_index * shard_size : (part_index + 1) * shard_size]
        part_path = _part_path(output_dir, part_index, shard, lost_digest, top_k)
        _remove_stale_parts(output_dir, part_index, keep=part_path)
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
        )

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
) -> None:
    part_started_at = perf_counter()
    columns = _empty_candidate_columns()
    for batch_start in range(0, len(shard), batch_size):
        batch = shard[batch_start : batch_start + batch_size]
        paths = [str(row.file_path) for row in batch]
        embeddings = np.asarray(model.generate_embeddings_batch(paths), dtype=np.float32)
        indices, scores = topk_cosine_similarity(embeddings, lost_embeddings, top_k=top_k)
        _append_candidates(columns, batch, lost_file_ids, indices, scores)
    write_auction_to_lost_rankings_parquet(part_path.name, **columns)
    logger.info(
        "Candidate part %d/%d written in %.1fs: rows=%d path=%s",
        part_number,
        part_count,
        perf_counter() - part_started_at,
        len(columns["auction_file_ids"]),
        part_path,
    )


def _append_candidates(columns, batch, lost_file_ids, indices, scores) -> None:
    for row_idx, row in enumerate(batch):
        for rank, lost_idx in enumerate(indices[row_idx], start=1):
            columns["auction_file_ids"].append(row.file_id)
            columns["lost_file_ids"].append(lost_file_ids[int(lost_idx)])
            columns["ranks"].append(rank)
            columns["blocking_scores"].append(float(scores[row_idx, rank - 1]))


def _part_path(output_dir: Path, index: int, shard, lost_digest: str, top_k: int) -> Path:
    digest = hashlib.sha256()
    digest.update(f"top_k={top_k}\nlost={lost_digest}\n".encode())
    for row in shard:
        digest.update(row.file_id.encode())
        digest.update(b"\n")
    return output_dir / f"part-{index:06d}-{digest.hexdigest()[:16]}.parquet"


def _digest_strings(values: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(str(value).encode())
        digest.update(b"\n")
    return digest.hexdigest()[:16]


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
        if index is not None and index >= start_index:
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
        "lost_file_ids": [],
        "ranks": [],
        "blocking_scores": [],
    }
