"""
Title confidence scoring based on lost_artwork title frequency.

The helper loads a global index of repeated titles once per process and then
reuses it for every lookup. A title that is not present in the index is treated
as unique and gets confidence 1.0.
"""

from __future__ import annotations

import math
import threading
from typing import Optional

from matching_pipeline.shared.db import connect as db_connect

TITLE_COUNT_INDEX: dict[str, int] = {}
TITLE_INDEX_TOTAL_COUNT = 0
TITLE_INDEX_LOADED = False
_INDEX_LOCK = threading.Lock()

def _normalize_title(title: str) -> str:
    return " ".join(title.split()).strip()

def _connect():
    return db_connect()

def load_title_index() -> None:
    """Load repeated titles from lost_artwork into a global in-memory index."""
    global TITLE_COUNT_INDEX, TITLE_INDEX_TOTAL_COUNT, TITLE_INDEX_LOADED

    if TITLE_INDEX_LOADED:
        return
    # Load the index in a thread-safe manner to avoid race conditions
    with _INDEX_LOCK:
        if TITLE_INDEX_LOADED:
            return

        title_counts: dict[str, int] = {}
        total_count = 0

        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT title, COUNT(*)::int AS title_count
                    FROM lost_artwork
                    WHERE title IS NOT NULL
                      AND btrim(title) <> ''
                    GROUP BY title
                    HAVING COUNT(*) > 1
                    ORDER BY COUNT(*) DESC, title ASC
                    """
                )
                for title, title_count in cur.fetchall():
                    title_counts[_normalize_title(str(title))] = int(title_count)

                cur.execute(
                    """
                    SELECT COUNT(*)::int
                    FROM lost_artwork
                    WHERE title IS NOT NULL
                      AND btrim(title) <> ''
                    """
                )
                total_count = int(cur.fetchone()[0] or 0)
        finally:
            conn.close()

        TITLE_COUNT_INDEX = title_counts
        TITLE_INDEX_TOTAL_COUNT = total_count
        TITLE_INDEX_LOADED = True


def _title_confidence(title: Optional[str]) -> float:
    if title is None or title.strip() == "":
        return 0.0

    normalized_title = _normalize_title(title)
    if not normalized_title:
        return 0.0

    if not TITLE_INDEX_LOADED:
        load_title_index()

    title_count = TITLE_COUNT_INDEX.get(normalized_title)
    if title_count is None:
        return 1.0

    if TITLE_INDEX_TOTAL_COUNT <= 1 or title_count <= 1:
        return 1.0

    score = math.log(TITLE_INDEX_TOTAL_COUNT / title_count) / math.log(TITLE_INDEX_TOTAL_COUNT)
    return max(0.0, min(1.0, float(score)))

# return the mean confidence of both titles in the 0..1 range.
def confidence_function(lost_title: Optional[str], auction_title: Optional[str]) -> float:
    
    if lost_title is None or lost_title.strip() == "" or auction_title is None or auction_title.strip() == "":
        return 0.0

    lost_confidence = _title_confidence(lost_title)
    auction_confidence = _title_confidence(auction_title)

    return (lost_confidence + auction_confidence) / 2.0