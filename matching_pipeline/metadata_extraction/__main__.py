"""Run metadata setup, extraction, and normalization as one pipeline stage."""

from __future__ import annotations

import logging
from pathlib import Path

from matching_pipeline.shared.db import connect as db_connect
from shared.logging_adapter import configure_logging

_PIPELINE_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)
DESCRIPTIONS_FILE = _PIPELINE_ROOT / "descriptions.jsonl"
UNMATCHED_FILE = _PIPELINE_ROOT / "descriptions_dating_unmatched.jsonl"


def _has_eligible_artworks() -> bool:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM auction_artwork
                    WHERE is_metadata_extraction_processed = false
                      AND is_image_matching_processed = true
                      AND description IS NOT NULL
                      AND btrim(description) <> ''
                    LIMIT 1
                )
                """
            )
            return bool(cur.fetchone()[0])


def run_extraction_normalization() -> None:
    from matching_pipeline.shared.env import get_model_config
    from matching_pipeline.metadata_extraction.run_extraction import run_extraction
    from matching_pipeline.metadata_normalization.run_normalization import run_normalization

    config = get_model_config()
    logger.info("Starting metadata extraction")
    run_extraction(descriptions_file=DESCRIPTIONS_FILE, backend=config.backend)
    logger.info("Metadata extraction complete; starting normalization")
    run_normalization(
        descriptions_file=DESCRIPTIONS_FILE,
        unmatched_file=UNMATCHED_FILE,
        backend=config.backend,
    )
    logger.info("Metadata normalization complete")


def main() -> int:
    configure_logging()
    if not _has_eligible_artworks():
        logger.info("No artworks eligible for metadata extraction; skipping stage.")
        return 0

    from matching_pipeline.metadata_normalization.technique_material_normalization.update_lost_artwork_dicts_once import (
        update_lost_artwork_dicts_once,
    )
    from matching_pipeline.shared.metadata_setup import run_setup_once

    run_setup_once()
    update_lost_artwork_dicts_once()
    run_extraction_normalization()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("Interrupted by user.")
