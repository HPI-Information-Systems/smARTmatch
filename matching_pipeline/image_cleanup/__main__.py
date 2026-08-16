"""Delete files for fully processed auction artworks that produced no match."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from matching_pipeline.image_cleanup.cleanup import (
    CleanupAlreadyRunning,
    CleanupBlockedByActiveScraper,
    CleanupBlockedByImageWriter,
    cleanup_unmatched_auction_images,
)
from matching_pipeline.shared.env import env_image_root
from shared.logging_adapter import configure_logging

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete eligible files. Without this flag, only report what would happen.",
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        help="Allowed image root (default: SMARTMATCH_IMAGES_DIR).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    image_root = args.images_dir or env_image_root()
    try:
        result = cleanup_unmatched_auction_images(
            image_root=image_root,
            apply=args.apply,
        )
    except CleanupAlreadyRunning:
        logger.info("Auction image cleanup skipped because another cleanup run holds the lock")
        return 0
    except CleanupBlockedByActiveScraper:
        logger.info("Auction image cleanup skipped because a scraper is running")
        return 0
    except CleanupBlockedByImageWriter:
        logger.info("Auction image cleanup skipped because the image store is being written")
        return 0
    except Exception:
        logger.exception("Auction image cleanup failed")
        return 1

    logger.info(
        "Auction image cleanup finished: mode=%s inventory_rows=%d "
        "candidate_rows=%d candidate_targets=%d protected_targets=%d "
        "would_delete=%d deleted=%d missing=%d cleaned_rows=%d unsafe=%d failed=%d bytes=%d",
        "apply" if result.apply else "dry-run",
        result.inventory_row_count,
        result.candidate_image_row_count,
        result.candidate_target_count,
        result.protected_target_count,
        result.would_delete_target_count,
        result.deleted_target_count,
        result.missing_target_count,
        result.cleaned_image_row_count,
        result.unsafe_target_count,
        result.failed_target_count,
        result.byte_count,
    )
    return 1 if args.apply and result.has_failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
