"""Standalone image-matching stage with Postgres result persistence."""

from __future__ import annotations

import logging
import os

from matching_pipeline.image_matching.config import matching_results_csv_path_from_env
from matching_pipeline.image_matching.db_results import write_matching_run_to_db
from matching_pipeline.image_matching.run_image_matching import run_image_matching
from shared.logging_adapter import configure_logging

_SKIP_ENV = "SMARTMATCH_SKIP_IMAGE_MATCHING"
logger = logging.getLogger(__name__)


def main() -> int:
    configure_logging()
    if os.getenv(_SKIP_ENV, "").strip() == "1":
        logger.warning("Image matching skipped because image blocking failed.")
        return 0

    result = run_image_matching(results_csv=matching_results_csv_path_from_env())
    db_result = write_matching_run_to_db(result)
    logger.info(
        "Image matching persisted: scores=%d processed_links=%d processed_artworks=%d",
        db_result.match_score_count,
        db_result.processed_auction_link_count,
        db_result.processed_auction_artwork_count,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("Interrupted by user.")
