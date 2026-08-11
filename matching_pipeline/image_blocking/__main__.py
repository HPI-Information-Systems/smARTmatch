"""CLI entrypoint for the recoverable file-backed image-blocking pipeline."""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from matching_pipeline.shared.artifacts import load_auction_to_lost_rankings_with_paths

from .config import (
    DEFAULT_CANDIDATE_SHARD_AUCTION_IMAGES,
    DEFAULT_EMBEDDING_DTYPE,
    DEFAULT_IMAGE_BATCH_SIZE,
    DEFAULT_TOP_K,
)
from .pipeline import BlockingRunResult, create_blocking_input_csv, run_image_blocking

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlockingCliResult:
    exit_code: int
    blocking_result: BlockingRunResult | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-csv", type=Path, help="CSV with file_id,file_path,role columns"
    )
    parser.add_argument(
        "--only-write-input-csv",
        dest="only_write_input_csv",
        action="store_true",
        help="Write a DB-derived input CSV under the blocking cache and exit.",
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--image-batch-size", type=int, default=DEFAULT_IMAGE_BATCH_SIZE)
    parser.add_argument(
        "--candidate-shard-auction-images",
        type=int,
        default=DEFAULT_CANDIDATE_SHARD_AUCTION_IMAGES,
    )
    parser.add_argument("--lost-limit", type=int)
    parser.add_argument(
        "--auction-limit",
        type=int,
        help="Maximum auction_artwork rows to process; defaults to MATCHING_BATCH_SIZE.",
    )
    parser.add_argument("--include-processed-auction-images", action="store_true")
    parser.add_argument(
        "--dtype", choices=["float16", "float32"], default=DEFAULT_EMBEDDING_DTYPE
    )
    parser.add_argument("--no-compile", action="store_true")
    parser.add_argument("--hf-token")
    parser.add_argument("--clear-candidates", action="store_true")
    parser.add_argument(
        "--log-level",
        default=os.getenv("BLOCKING_LOG_LEVEL", "INFO"),
        help="Blocking log level: DEBUG, INFO, WARNING, ERROR, or CRITICAL.",
    )
    return parser


def parse_blocking_args_and_run_blocking(*, full_pipeline: bool = False) -> int:
    return parse_blocking_args_and_run_blocking_with_result(
        full_pipeline=full_pipeline
    ).exit_code


def parse_blocking_args_and_run_blocking_with_result(
    *, full_pipeline: bool = False
) -> BlockingCliResult:
    parser = build_parser()
    args = parser.parse_args()
    try:
        _configure_logging(args.log_level)
    except ValueError as exc:
        parser.error(str(exc))
    if full_pipeline:
        logger.info("Running full image pipeline: blocking + LightGlue matching")
    else:
        logger.info("Running blocking only; downstream LightGlue matching will not start")
    if args.only_write_input_csv and args.input_csv is not None:
        parser.error("--only-write-input-csv cannot be used with --input-csv")
    if args.only_write_input_csv:
        result = create_blocking_input_csv(
            lost_limit=args.lost_limit,
            auction_limit=args.auction_limit,
            include_processed_auction_images=args.include_processed_auction_images,
        )
        print(f"Input CSV: {result.input_csv}")
        print(f"Lost images: {result.lost_image_count}")
        print(f"Auction images: {result.auction_image_count}")
        return BlockingCliResult(exit_code=0)

    kwargs = vars(args)
    kwargs.pop("only_write_input_csv")
    kwargs.pop("log_level")
    result = run_image_blocking(**kwargs)
    _print_blocking_result(result)
    return BlockingCliResult(exit_code=0, blocking_result=result)


def _print_blocking_result(result: BlockingRunResult) -> None:
    print(f"Blocking cache: {result.cache_dir}")
    print(f"Lost images: {result.lost_image_count}")
    print(f"Auction images: {result.auction_image_count}")
    print(f"Generated lost embeddings: {result.generated_lost_embedding_count}")
    print(f"Embedded image files marked: {result.embedded_image_file_count}")
    print(f"Candidate parts skipped: {result.skipped_candidate_part_count}")
    print(
        f"Candidates: {result.candidate_count} rows in {result.candidate_part_count} parts"
    )
    _print_ranking_preview()


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, str(level_name).upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"Invalid log level: {level_name!r}")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


def _print_ranking_preview(limit: int = 10) -> None:
    rows = _ranking_preview_rows(limit)
    if not rows:
        print("Ranking preview: no candidate rankings found.")
        return
    print("\nFirst candidate rankings:")
    _print_table(("#", "auction_file_id", "rank", "lost_file_id"), rows)


def _ranking_preview_rows(limit: int) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for item in load_auction_to_lost_rankings_with_paths():
        for rank, candidate in enumerate(item["match_candidates"], start=1):
            rows.append(
                (
                    str(len(rows) + 1),
                    item["auction_file_id"],
                    str(rank),
                    candidate["lost_file_id"],
                )
            )
            if len(rows) >= limit:
                return rows
    return rows


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(value)) for width, value in zip(widths, row)]
    print(" | ".join(value.ljust(width) for value, width in zip(headers, widths)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(value.ljust(width) for value, width in zip(row, widths)))


if __name__ == "__main__":
    raise SystemExit(parse_blocking_args_and_run_blocking())
