"""Run metadata matching for eligible auction artworks."""

from __future__ import annotations

import logging
from matching_pipeline.shared.db import connect as db_connect
from shared.logging_adapter import configure_logging

logger = logging.getLogger(__name__)


def _has_eligible_artworks() -> bool:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM auction_artwork
                    WHERE is_metadata_matching_processed = false
                      AND is_metadata_extraction_processed = true
                      AND is_image_matching_processed = true
                    LIMIT 1
                )
                """
            )
            return bool(cur.fetchone()[0])


def run_metadata_matching():
    if not _has_eligible_artworks():
        logger.info("No artworks eligible for metadata matching; skipping stage.")
        return {
            "lost_loaded": 0,
            "auction_pairs_processed": 0,
            "elapsed_seconds": 0.0,
        }

    from matching_pipeline.metadata_normalization.technique_material_normalization.update_lost_artwork_dicts_once import (
        update_lost_artwork_dicts_once,
    )
    from matching_pipeline.shared.metadata_setup import run_setup_once
    from matching_pipeline.metadata_matching.main_matcher.metadata_matcher import match_metadata

    run_setup_once()
    update_lost_artwork_dicts_once()
    return match_metadata()


def main() -> int:
    configure_logging()
    run_metadata_matching()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("Interrupted by user.")
