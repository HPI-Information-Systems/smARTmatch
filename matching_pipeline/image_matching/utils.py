"""CSV serialization helpers for image-matching result rows."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Sequence

from matching_pipeline.image_matching.results import AcceptedImageMatch

MATCH_CSV_FIELDNAMES = [
    "auction_file_id",
    "auction_file_path",
    "lost_file_id",
    "lost_file_path",
    "confidence",
    "blocking_score",
]


def save_matches_to_csv(matches: Sequence[AcceptedImageMatch], csv_path: Path) -> None:
    path = Path(csv_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [match.as_csv_row() for match in matches]
    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=MATCH_CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
