"""
Applies normalize_names._normalize to each record's "author" field in place,
filling name/date/place fields only where they aren't already set.
"""

import json
import logging
from pathlib import Path

from matching_pipeline.metadata_normalization.artist_normalization.normalize_names import _normalize

logger = logging.getLogger(__name__)


def run_artist_normalization(descriptions_file: Path) -> None:
    records = []
    with open(descriptions_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    n_matched = n_unmatched = n_empty = 0

    for rec in records:
        author = (rec.get("author") or "").strip()
        if not author:
            n_empty += 1
            continue

        result = _normalize(rec["id"], author)

        if result is None or not result.get("matched"):
            n_unmatched += 1
            continue

        if result.get("artist_full_name"):
            rec["author"] = result["artist_full_name"]

        # Fill date/place fields only if qwen left them empty
        for src, dst in [
            ("date_of_birth_raw_data", "date_of_birth"),
            ("date_of_death_raw_data", "date_of_death"),
            ("place_of_birth_raw_data", "place_of_birth"),
            ("place_of_death_raw_data", "place_of_death"),
        ]:
            if not rec.get(dst) and result.get(src):
                rec[dst] = result[src]

        n_matched += 1

    with open(descriptions_file, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    total = n_matched + n_unmatched + n_empty
    logger.info(f"Matched:   {n_matched} / {total}")
    logger.info(f"Unmatched: {n_unmatched}")
    logger.info(f"Empty:     {n_empty}")


if __name__ == "__main__":
    _ROOT = Path(__file__).parent.parent.parent
    run_artist_normalization(descriptions_file=_ROOT / "descriptions.jsonl")
