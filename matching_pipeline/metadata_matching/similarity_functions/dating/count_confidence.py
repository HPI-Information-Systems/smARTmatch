""""
This module implements a confidence function for dating metadata, 
based on the IDF-like scoring of dating ranges in the lost_artwork table. 
"""

import pickle
from pathlib import Path

import numpy as np

from matching_pipeline.shared.db import connect as db_connect

_CACHE_DIR = Path(__file__).parent / "__pycache__"
_DATING_CACHE = _CACHE_DIR / "dating_state.pkl"

_dating_state = None


def _load_dating():
    global _dating_state
    if _DATING_CACHE.exists():
        with open(_DATING_CACHE, "rb") as f:
            _dating_state = pickle.load(f)
        return

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT dating_start, dating_end
                FROM lost_artwork
                WHERE dating_start IS NOT NULL
                  AND dating_end IS NOT NULL;
                """
            )
            rows = cur.fetchall()

    # coverage[year] = number of lost_artwork dating ranges spanning that year
    year_min = min(s for s, _ in rows)
    year_max = max(e for _, e in rows)
    coverage = np.zeros(year_max - year_min + 1, dtype=np.int32)
    for start, end in rows:
        coverage[start - year_min : end - year_min + 1] += 1

    # IDF per year: common years get a low score, rare years a high one.
    N = len(rows)
    safe_coverage = np.where(coverage > 0, coverage, 1)
    idf = np.log(N / safe_coverage.astype(float))

    _dating_state = {
        "coverage": coverage,
        "idf": idf,
        "year_min": year_min,
        "year_max": year_max,
        "log_N": np.log(N),
    }
    with open(_DATING_CACHE, "wb") as f:
        pickle.dump(_dating_state, f)


def _score_dating(dating_start: int, dating_end: int) -> float:
    if _dating_state is None:
        _load_dating()

    s = _dating_state
    # Clip the range to the years we have IDF data for.
    lo = max(dating_start, s["year_min"])
    hi = min(dating_end, s["year_max"])
    if lo > hi:
        # Outside known years -> treat as maximally confident.
        return 1.0

    # Average IDF over the range, normalized to [0, 1].
    length = dating_end - dating_start + 1
    raw = s["idf"][lo - s["year_min"] : hi - s["year_min"] + 1].sum() / length
    return float(np.clip(raw / s["log_N"], 0.0, 1.0))


def confidence_function(
    lost_dating_start, lost_dating_end, auction_dating_start, auction_dating_end
):

    if (
        lost_dating_start is None
        or lost_dating_end is None
        or auction_dating_start is None
        or auction_dating_end is None
    ):
        return 0.0

    lost_score = _score_dating(lost_dating_start, lost_dating_end)
    auction_score = _score_dating(auction_dating_start, auction_dating_end)
    return (lost_score + auction_score) / 2.0
