"""Read and write auction-to-lost top-k ranking Parquet artifacts."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TypedDict

from matching_pipeline.shared.env import env_auction_to_lost_rankings_dir

from .image_files import read_image_files_parquet
from .parquet_common import require_pyarrow, write_table_atomic

DEFAULT_LOAD_BATCH_SIZE = 1_000
_RANKING_ROW_BATCH_SIZE = 1_000


class LostMatchCandidate(TypedDict):
    lost_file_id: str
    lost_file_path: str
    blocking_score: float


class AuctionMatchCandidates(TypedDict):
    auction_file_id: str
    auction_file_path: str
    match_candidates: list[LostMatchCandidate]


class RankingArtifactSummary(TypedDict):
    part_count: int
    row_count: int
    auction_file_count: int


def write_auction_to_lost_rankings_parquet(
    part_name: str,
    *,
    auction_file_ids: Sequence[str],
    auction_content_versions: Sequence[int | None],
    auction_content_sha256: Sequence[str],
    lost_file_ids: Sequence[str],
    lost_content_versions: Sequence[int | None],
    lost_content_sha256: Sequence[str],
    ranks: Sequence[int],
    blocking_scores: Sequence[float],
) -> Path:
    """Write one top-k auction-to-lost ranking part under `CACHE_DIR`."""
    output_path = _ranking_part_path(part_name)
    _validate_equal_lengths(
        auction_file_ids,
        auction_content_versions,
        auction_content_sha256,
        lost_file_ids,
        lost_content_versions,
        lost_content_sha256,
        ranks,
        blocking_scores,
    )
    coerced_auction_ids = [
        _required_text(value, "auction_file_id") for value in auction_file_ids
    ]
    coerced_auction_versions = [
        _coerce_content_version(value, "auction_content_version")
        for value in auction_content_versions
    ]
    coerced_auction_digests = [
        _coerce_content_sha256(value, "auction_content_sha256")
        for value in auction_content_sha256
    ]
    coerced_lost_ids = [_required_text(value, "lost_file_id") for value in lost_file_ids]
    coerced_lost_versions = [
        _coerce_content_version(value, "lost_content_version")
        for value in lost_content_versions
    ]
    coerced_lost_digests = [
        _coerce_content_sha256(value, "lost_content_sha256")
        for value in lost_content_sha256
    ]
    coerced_ranks = [_coerce_rank(value) for value in ranks]
    coerced_scores = [_coerce_score(value) for value in blocking_scores]

    pa, pq = require_pyarrow()
    table = pa.table(
        {
            "auction_file_id": coerced_auction_ids,
            "auction_content_version": coerced_auction_versions,
            "auction_content_sha256": coerced_auction_digests,
            "lost_file_id": coerced_lost_ids,
            "lost_content_version": coerced_lost_versions,
            "lost_content_sha256": coerced_lost_digests,
            "rank": coerced_ranks,
            "blocking_score": coerced_scores,
        },
        schema=pa.schema(
            [
                ("auction_file_id", pa.string()),
                ("auction_content_version", pa.int64()),
                ("auction_content_sha256", pa.string()),
                ("lost_file_id", pa.string()),
                ("lost_content_version", pa.int64()),
                ("lost_content_sha256", pa.string()),
                ("rank", pa.int16()),
                ("blocking_score", pa.float32()),
            ]
        ),
    )
    write_table_atomic(pq, table, output_path)
    return output_path


def summarize_auction_to_lost_rankings() -> RankingArtifactSummary:
    """Return counts for current auction-to-lost ranking artifacts."""
    paths = _ranking_part_paths()
    if not paths:
        return {"part_count": 0, "row_count": 0, "auction_file_count": 0}

    _pa, pq = require_pyarrow()
    row_count = 0
    auction_file_ids: set[str] = set()
    for path in paths:
        parquet_file = pq.ParquetFile(path)
        auction_column, _lost_column = _ranking_id_columns(
            parquet_file.schema_arrow.names, path
        )
        row_count += int(parquet_file.metadata.num_rows)
        for batch in parquet_file.iter_batches(
            batch_size=_RANKING_ROW_BATCH_SIZE, columns=[auction_column]
        ):
            names = batch.schema.names
            auction_values = batch.column(names.index(auction_column)).to_pylist()
            auction_file_ids.update(
                _required_text(value, auction_column) for value in auction_values
            )
    return {
        "part_count": len(paths),
        "row_count": row_count,
        "auction_file_count": len(auction_file_ids),
    }


def load_auction_to_lost_rankings_with_paths(
    *, batch_size: int = DEFAULT_LOAD_BATCH_SIZE
) -> Iterator[AuctionMatchCandidates]:
    """Stream grouped ranking artifacts joined with image paths from `CACHE_DIR`."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    paths = _ranking_part_paths()
    if not paths:
        return

    auction_paths = read_image_files_parquet("auction")
    lost_paths = read_image_files_parquet("lost")
    batch: list[AuctionMatchCandidates] = []
    for item in _iter_rankings(paths, auction_paths, lost_paths):
        batch.append(item)
        if len(batch) == batch_size:
            yield from batch
            batch.clear()
    if batch:
        yield from batch


def _iter_rankings(
    paths: Sequence[Path], auction_paths: dict[str, str], lost_paths: dict[str, str]
) -> Iterator[AuctionMatchCandidates]:
    _pa, pq = require_pyarrow()
    current_auction_id: str | None = None
    current_candidates: list[tuple[int, str, float]] = []
    seen_auction_ids: set[str] = set()

    for auction_id, lost_id, rank, blocking_score in _iter_ranking_rows(pq, paths):
        if auction_id != current_auction_id:
            if current_auction_id is not None:
                yield _make_item(current_auction_id, current_candidates, auction_paths, lost_paths)
                seen_auction_ids.add(current_auction_id)
            if auction_id in seen_auction_ids:
                raise ValueError(
                    f"Ranking rows for auction_file_id are not contiguous: {auction_id}"
                )
            current_auction_id = auction_id
            current_candidates = []
        current_candidates.append((rank, lost_id, blocking_score))

    if current_auction_id is not None:
        yield _make_item(current_auction_id, current_candidates, auction_paths, lost_paths)


def _iter_ranking_rows(pq, paths: Sequence[Path]) -> Iterator[tuple[str, str, int, float]]:
    for path in paths:
        parquet_file = pq.ParquetFile(path)
        auction_column, lost_column = _ranking_id_columns(parquet_file.schema_arrow.names, path)
        columns = [auction_column, lost_column, "rank", "blocking_score"]
        for batch in parquet_file.iter_batches(
            batch_size=_RANKING_ROW_BATCH_SIZE, columns=columns
        ):
            names = batch.schema.names
            auction_values = batch.column(names.index(auction_column)).to_pylist()
            lost_values = batch.column(names.index(lost_column)).to_pylist()
            rank_values = batch.column(names.index("rank")).to_pylist()
            blocking_values = batch.column(names.index("blocking_score")).to_pylist()
            for auction_id, lost_id, rank, blocking_score in zip(
                auction_values,
                lost_values,
                rank_values,
                blocking_values,
            ):
                yield (
                    _required_text(auction_id, auction_column),
                    _required_text(lost_id, lost_column),
                    _coerce_rank(rank),
                    _coerce_score(blocking_score),
                )


def _make_item(
    auction_id: str,
    candidates: Sequence[tuple[int, str, float]],
    auction_paths: dict[str, str],
    lost_paths: dict[str, str],
) -> AuctionMatchCandidates:
    return {
        "auction_file_id": auction_id,
        "auction_file_path": _lookup_path(auction_paths, auction_id, "auction"),
        "match_candidates": [
            {
                "lost_file_id": lost_id,
                "lost_file_path": _lookup_path(lost_paths, lost_id, "lost"),
                "blocking_score": blocking_score,
            }
            for _rank, lost_id, blocking_score in sorted(
                candidates, key=lambda candidate: candidate[0]
            )
        ],
    }


def _ranking_part_path(part_name: str) -> Path:
    if not part_name or Path(part_name).name != part_name:
        raise ValueError(f"Ranking part name must be a file name, got {part_name!r}")
    return env_auction_to_lost_rankings_dir() / part_name


def _ranking_part_paths() -> list[Path]:
    rankings_dir = env_auction_to_lost_rankings_dir()
    if not rankings_dir.is_dir():
        raise FileNotFoundError(f"Ranking artifact directory not found: {rankings_dir}")
    return sorted(rankings_dir.glob("part-*.parquet"))


def _ranking_id_columns(names: Sequence[str], path: Path) -> tuple[str, str]:
    required = {"auction_file_id", "lost_file_id", "rank", "blocking_score"}
    missing = sorted(required - set(names))
    if missing:
        raise ValueError(
            f"Ranking artifact {path} is missing columns: {', '.join(missing)}"
        )
    return "auction_file_id", "lost_file_id"


def _lookup_path(paths: dict[str, str], file_id: str, role: str) -> str:
    try:
        return paths[file_id]
    except KeyError as exc:
        raise ValueError(f"Ranking artifact references unknown {role} file_id: {file_id}") from exc


def _validate_equal_lengths(*columns: Sequence[object]) -> None:
    lengths = {len(column) for column in columns}
    if len(lengths) != 1:
        raise ValueError(f"Ranking columns have different lengths: {sorted(lengths)}")


def _required_text(value: object, field_name: str) -> str:
    if value is None:
        raise ValueError(f"Missing {field_name} in ranking artifact")
    text = str(value).strip()
    if not text:
        raise ValueError(f"Empty {field_name} in ranking artifact")
    return text


def _coerce_content_version(value: object, field_name: str) -> int | None:
    if value is None or not str(value).strip():
        return None
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if version <= 0:
        raise ValueError(f"{field_name} must be positive: {version}")
    return version


def _coerce_content_sha256(value: object, field_name: str) -> str:
    digest = _required_text(value, field_name).lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(
            f"{field_name} must contain 64 hexadecimal characters: {digest!r}"
        )
    return digest


def _coerce_rank(value: object) -> int:
    try:
        rank = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid ranking rank: {value!r}") from exc
    if rank <= 0 or rank > 32_767:
        raise ValueError(f"Ranking rank must be in [1, 32767]: {rank}")
    return rank


def _coerce_score(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid blocking_score: {value!r}") from exc
