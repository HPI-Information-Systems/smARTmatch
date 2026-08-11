""""
This module implements a similarity function for title attributes.

The similarity function uses a levenshtein matcher to compute a score between two titles
and also checks for exact matches against a list of common titles with pre-defined, IDF-scores.
"""

import csv
from pathlib import Path
from typing import Dict, Optional

from matching_pipeline.metadata_matching.similarity_functions.title.levenshtein_matcher import similarity_function as special_similarity_function

COMMON_TITLE_SCORES: Dict[str, float] = {}
IS_COMMON_TITLES_LOADED = False
METADATA_MATCHING_ROOT = Path(__file__).resolve().parents[1]
COMMON_TITLES_CSV = Path(__file__).resolve().parent / "common_titles.csv"

def load_common_titles() -> None:
    global COMMON_TITLE_SCORES, IS_COMMON_TITLES_LOADED

    if IS_COMMON_TITLES_LOADED:
        return

    if not COMMON_TITLES_CSV.exists():
        raise FileNotFoundError(f"Common titles CSV not found: {COMMON_TITLES_CSV}")

    loaded_scores: Dict[str, float] = {}
    with open(COMMON_TITLES_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            title_name = row.get("title_name")
            title_score = row.get("title_score")

            if not title_name or not title_score:
                continue

            try:
                loaded_scores[title_name] = float(title_score)
            except ValueError:
                continue

    COMMON_TITLE_SCORES = loaded_scores
    IS_COMMON_TITLES_LOADED = True


def similarity_function(
    lost_title: Optional[str], auction_title: Optional[str]
) -> Optional[float]:

    if auction_title is None or lost_title is None:
        return None

    if auction_title.lower().strip() == lost_title.lower().strip():
        load_common_titles()
        score = COMMON_TITLE_SCORES.get(auction_title.lower().strip())
        if score is not None:
            return score
        return 1.0

    return special_similarity_function(auction_title, lost_title)

