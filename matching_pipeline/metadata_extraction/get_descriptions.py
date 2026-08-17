"""
Fetches a batch of unprocessed auction_artwork descriptions from the DB,
prioritizing ones still awaiting a metadata match score, and writes them
to output_file as JSONL scaffolded with empty extraction fields.
"""

import json
import logging
import os
from pathlib import Path

from matching_pipeline.metadata_extraction.status import EXTRACTION_PARSED_FIELD
from matching_pipeline.shared.db import connect as db_connect

logger = logging.getLogger(__name__)

_BATCH_SIZE = int(os.environ.get("MATCHING_BATCH_SIZE", 100))

def get_descriptions(output_file: Path) -> None:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.auction_artwork_id, a.description
                FROM auction_artwork a
                WHERE a.is_metadata_extraction_processed = false
                AND a.is_image_matching_processed = true
                AND a.description IS NOT NULL AND a.description != ''
                ORDER BY (
                    EXISTS (
                        SELECT 1 FROM match_score m
                        WHERE m.auction_id = a.auction_artwork_id
                        AND m.metadata_final_score IS NULL
                        AND m.image_final_score IS NOT NULL
                    )
                ) DESC
                LIMIT %s
                """,
                (_BATCH_SIZE,),
            )
            rows = cur.fetchall()
            records = [
                {
                    "id": str(r[0]),
                    "description": r[1],
                    "title": "",
                    "author": "",
                    "date_of_birth": "",
                    "place_of_birth": "",
                    "date_of_death": "",
                    "place_of_death": "",
                    "dimensions": "",
                    "dating": "",
                    "material": "",
                    "technique": "",
                    "provenance": "",
                    "signature": "",
                    "condition": "",
                    "literature": "",
                    "dating_start": None,
                    "dating_end": None,
                    EXTRACTION_PARSED_FIELD: False,
                }
                for r in rows
            ]

    with open(output_file, "w", encoding="utf-8") as f:
        for item in records:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    logger.info(f"Fetched:  {len(records)}")
    logger.info(f"Output:   {output_file}")
