"""Persist successfully parsed metadata-normalization records.

Records with malformed or unparseable LLM output remain extraction-pending so
a later pipeline cycle retries them. Valid parsed records with an empty entity
schema are marked processed because they have no extractable metadata.
"""

import json
import logging
from pathlib import Path
from typing import Any

from matching_pipeline.metadata_extraction.status import EXTRACTION_PARSED_FIELD
from matching_pipeline.shared.db import connect as db_connect

logger = logging.getLogger(__name__)

_METADATA_PAYLOAD_FIELDS = (
    "title",
    "author",
    "dating",
    "material",
    "technique",
    "provenance",
    "signature",
    "condition",
    "literature",
    "dimensions",
    "date_of_birth",
    "date_of_death",
    "place_of_birth",
    "place_of_death",
)

_SQL = """
UPDATE auction_artwork SET
    title                    = %s,
    artist_full_name         = %s,
    dating                   = %s,
    dating_start             = %s,
    dating_end               = %s,
    material                 = %s,
    dict_material_name       = %s,
    technique                = %s,
    dict_technique_name      = %s,
    provenance               = %s,
    signature                = %s,
    condition                = %s,
    literature               = %s,
    width                    = %s,
    height                   = %s,
    width_frame              = %s,
    height_frame             = %s,
    dimensions_raw_data      = %s,
    date_of_birth_raw_data   = %s,
    date_of_death_raw_data   = %s,
    place_of_birth_raw_data  = %s,
    place_of_death_raw_data  = %s,
    is_metadata_extraction_processed = true
WHERE auction_artwork_id = %s
"""


_VARCHAR_255_LIMIT = 255


def _s(val: Any) -> "str | None":
    if not val:
        return None
    s = str(val).strip()
    return s or None


def _s255(val: Any, *, field: str, record_id: Any) -> "str | None":
    s = _s(val)
    if s is not None and len(s) > _VARCHAR_255_LIMIT:
        logger.warning(
            f"Truncating {field} for record {record_id}: "
            f"{len(s)} chars exceeds varchar(255) limit"
        )
        s = s[:_VARCHAR_255_LIMIT]
    return s


def _f(val: Any) -> "float | None":
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _i(val: Any) -> "int | None":
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _has_metadata_payload(rec: dict[str, Any]) -> bool:
    return any(_s(rec.get(field)) is not None for field in _METADATA_PAYLOAD_FIELDS)


def _record_was_parsed(rec: dict[str, Any]) -> bool:
    if EXTRACTION_PARSED_FIELD in rec:
        return rec.get(EXTRACTION_PARSED_FIELD) is True
    return _has_metadata_payload(rec)


def write_descriptions(descriptions_file: Path) -> None:
    records = []
    malformed_line_count = 0
    with open(descriptions_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed_line_count += 1
                continue
            if not isinstance(record, dict):
                malformed_line_count += 1
                continue
            records.append(record)

    logger.info(f"Loaded {len(records)} records from {descriptions_file}")

    parsed_records = [
        rec
        for rec in records
        if _s(rec.get("id")) is not None and _record_was_parsed(rec)
    ]
    records_with_payload = [
        rec for rec in parsed_records if _has_metadata_payload(rec)
    ]
    records_without_payload = [
        rec for rec in parsed_records if not _has_metadata_payload(rec)
    ]
    retry_count = len(records) - len(parsed_records) + malformed_line_count

    rows = [
        (
            _s(rec.get("title")),
            _s255(rec.get("author"), field="author", record_id=rec.get("id")),
            _s255(rec.get("dating"), field="dating", record_id=rec.get("id")),
            _i(rec.get("dating_start")),
            _i(rec.get("dating_end")),
            _s255(rec.get("material"), field="material", record_id=rec.get("id")),
            rec.get("dict_material_name"),
            _s255(rec.get("technique"), field="technique", record_id=rec.get("id")),
            rec.get("dict_technique_name"),
            _s(rec.get("provenance")),
            _s(rec.get("signature")),
            _s(rec.get("condition")),
            _s(rec.get("literature")),
            _f(rec.get("dim_width")),
            _f(rec.get("dim_height")),
            _f(rec.get("dim_width_frame")),
            _f(rec.get("dim_height_frame")),
            _s(rec.get("dimensions")),
            _s(rec.get("date_of_birth")),
            _s(rec.get("date_of_death")),
            _s(rec.get("place_of_birth")),
            _s(rec.get("place_of_death")),
            rec["id"],
        )
        for rec in records_with_payload
    ]
    empty_ids = [rec["id"] for rec in records_without_payload]

    with db_connect() as conn:
        with conn.cursor() as cur:
            if rows:
                cur.executemany(_SQL, rows)
            if empty_ids:
                cur.execute(
                    "UPDATE auction_artwork SET is_metadata_extraction_processed = true WHERE auction_artwork_id = ANY(%s)",
                    (empty_ids,),
                )
        conn.commit()

    logger.info(f"Written: {len(rows)} rows with extracted metadata")
    if empty_ids:
        logger.info(f"Marked:  {len(empty_ids)} rows as processed (no extractable entities)")
    if retry_count:
        logger.warning(
            "Left %d metadata extraction records pending for retry",
            retry_count,
        )


if __name__ == "__main__":
    _ROOT = Path(__file__).parent.parent
    write_descriptions(descriptions_file=_ROOT / "descriptions.jsonl")
