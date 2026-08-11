"""
Artist confidence scoring based on artist-name document frequency.

This module builds a global index of artist complete_name -> document frequency,
where document frequency means: in how many `lost_artwork` rows the artist name
appears.
"""

from __future__ import annotations

import csv
import math
import threading
import unicodedata
from pathlib import Path
from typing import Any, Optional

from matching_pipeline.shared.db import connect as db_connect


ARTIST_COUNT_INDEX: dict[str, int] = {}
ARTIST_INDEX_TOTAL_DOCS = 0
ARTIST_INDEX_LOADED = False
COMMON_ARTISTS_CSV = Path(__file__).resolve().parent / "common_artists.csv"

# lock for thread-safe access to ARTIST_COUNT_INDEX and ARTIST_INDEX_TOTAL_DOCS
_INDEX_LOCK = threading.Lock()


_ARTIST_LOOKUP_INDEX: dict[str, str] = {}
_ARTIST_LOOKUP_INDEX_SOURCE_ID: int | None = None

UNKNOWN_ARTIST_NAMES = {"unknown", "unbekannt", "unidentified", "anoniem", "anonyme", "anonymus", "anonym" }


def _connect():
    return db_connect()


def _normalize_name(name: Any) -> str:
    if name is None:
        return ""

    value = unicodedata.normalize("NFC", str(name))
    return " ".join(value.strip().split())


def _lookup_key(name: Any) -> str:
    return _normalize_name(name).casefold()


def _is_unknown_artist_name(name: Any) -> bool:
    return _lookup_key(name) in UNKNOWN_ARTIST_NAMES


def _split_pg_array_literal(value: str) -> list[str]:
    """Parse a simple PostgreSQL array literal into string items.

    Handles both `{Jan Matejko,Rembrandt van Rijn}` and quoted items such as
    `{"Klee, Paul","Jan Matejko"}`. In normal psycopg usage array columns
    usually arrive as Python lists already; this is mainly a defensive parser.
    """
    inner = value.strip()[1:-1]
    if not inner:
        return []

    try:
        return [
            part
            for part in next(
                csv.reader(
                    [inner],
                    delimiter=",",
                    quotechar='"',
                    escapechar="\\",
                    skipinitialspace=True,
                )
            )
        ]
    except csv.Error:
        return [part.strip() for part in inner.split(",")]


def _extract_names(value: Any) -> list[str]:
    """Extract normalized artist names from DB/Python values.

    Supported inputs:
    - None -> []
    - list/tuple/set, including psycopg text[] values
    - PostgreSQL array literal: `{Jan Matejko,"Klee, Paul"}`
    - semicolon/pipe-separated strings: `Jan Matejko; Rembrandt van Rijn`

    Ordinary strings are not split by comma, because `Surname, Forename` is a
    valid artist-name format.
    """
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        names: list[str] = []
        for item in value:
            names.extend(_extract_names(item))
        return names

    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return []

        if raw_value.startswith("{") and raw_value.endswith("}"):
            raw_parts = _split_pg_array_literal(raw_value)
        else:
            raw_parts = [raw_value]
            for separator in [";", "|"]:
                if separator in raw_value:
                    raw_parts = raw_value.split(separator)
                    break

        names = []
        for raw_part in raw_parts:
            name = _normalize_name(raw_part)
            if name and not _is_unknown_artist_name(name):
                names.append(name)
        return names

    name = _normalize_name(value)
    if not name or _is_unknown_artist_name(name):
        return []
    return [name]

# return IDF-like confidence in [0, 1] for one artist name
def _artist_score(total_docs: int, artist_count: int) -> float:
    if total_docs <= 1 or artist_count <= 1:
        return 1.0

    score = math.log(total_docs / artist_count) / math.log(total_docs)
    return max(0.0, min(1.0, float(score)))


def _rebuild_lookup_index() -> None:
    global _ARTIST_LOOKUP_INDEX, _ARTIST_LOOKUP_INDEX_SOURCE_ID
    _ARTIST_LOOKUP_INDEX = {
        _lookup_key(artist_name): artist_name
        for artist_name in ARTIST_COUNT_INDEX
        if artist_name and not _is_unknown_artist_name(artist_name)
    }
    _ARTIST_LOOKUP_INDEX_SOURCE_ID = id(ARTIST_COUNT_INDEX)


def _write_common_artists_csv(counts: dict[str, int], total_docs: int) -> Path:
    COMMON_ARTISTS_CSV.parent.mkdir(parents=True, exist_ok=True)

    with COMMON_ARTISTS_CSV.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(
            file_obj,
            fieldnames=["artist_name", "artist_count", "artist_score"],
            delimiter=";",
        )
        writer.writeheader()

        for artist_name, artist_count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        ):
            writer.writerow(
                {
                    "artist_name": artist_name,
                    "artist_count": int(artist_count),
                    "artist_score": _artist_score(total_docs, int(artist_count)),
                }
            )

    return COMMON_ARTISTS_CSV


def load_artist_index() -> None:
    """Load artist-name document frequencies into the global index.

    Index shape after loading:

        ARTIST_COUNT_INDEX = {
            "Jan Matejko": 12,
            "Rembrandt van Rijn": 3,
        }

    Counts are document frequencies. One lost_artwork row contributes at most
    +1 to a given normalized artist name, even if duplicate IDs/names appear in
    that record.
    """
    global ARTIST_COUNT_INDEX, ARTIST_INDEX_TOTAL_DOCS, ARTIST_INDEX_LOADED

    # locks for multi-threaded access to ARTIST_COUNT_INDEX and ARTIST_INDEX_TOTAL_DOCS
    if ARTIST_INDEX_LOADED:
        return

    with _INDEX_LOCK:
        if ARTIST_INDEX_LOADED:
            return

        counts: dict[str, int] = {}
        display_name_by_key: dict[str, str] = {}
        total_docs = 0

        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        (
                            SELECT array_agg(a.complete_name ORDER BY a.complete_name)
                            FROM artist a
                            WHERE a.artist_id = ANY(l.artist_ids)
                              AND a.complete_name IS NOT NULL
                              AND btrim(a.complete_name) <> ''
                        ) AS artist_names
                    FROM lost_artwork l
                    WHERE l.artist_ids IS NOT NULL
                      AND cardinality(l.artist_ids) > 0
                    """
                )
                rows = cur.fetchall()

            for (artist_names,) in rows:
                names = _extract_names(artist_names)
                if not names:
                    continue

                total_docs += 1
                seen_in_document: set[str] = set()

                for name in names:
                    key = _lookup_key(name)
                    if not key or key in seen_in_document:
                        continue

                    display_name = display_name_by_key.setdefault(key, name)
                    counts[display_name] = counts.get(display_name, 0) + 1
                    seen_in_document.add(key)
        finally:
            conn.close()

        ARTIST_COUNT_INDEX = counts
        ARTIST_INDEX_TOTAL_DOCS = total_docs
        ARTIST_INDEX_LOADED = True
        _rebuild_lookup_index()

        _write_common_artists_csv(counts, total_docs)


def _get_artist_count(artist_name: str) -> Optional[int]:
    normalized_name = _normalize_name(artist_name)

    direct_count = ARTIST_COUNT_INDEX.get(normalized_name)
    if direct_count is not None:
        return int(direct_count)

    key = _lookup_key(normalized_name)
    display_name = _ARTIST_LOOKUP_INDEX.get(key)

    # Supports tests/manual state injection where ARTIST_COUNT_INDEX is set
    # directly and _ARTIST_LOOKUP_INDEX is stale or empty. Only rebuild when
    # ARTIST_COUNT_INDEX was actually replaced since the last rebuild -
    # otherwise a name that's genuinely absent would trigger a full rebuild
    # on every single call (catastrophic when called per lost/auction pair).
    if (
        display_name is None
        and ARTIST_COUNT_INDEX
        and _ARTIST_LOOKUP_INDEX_SOURCE_ID != id(ARTIST_COUNT_INDEX)
    ):
        _rebuild_lookup_index()
        display_name = _ARTIST_LOOKUP_INDEX.get(key)

    if display_name is None:
        return None

    artist_count = ARTIST_COUNT_INDEX.get(display_name)
    return int(artist_count) if artist_count is not None else None


def _artist_confidence(artist_name: Optional[str]) -> float:
    if artist_name is None:
        return 0.0

    name = _normalize_name(artist_name)
    if not name or _is_unknown_artist_name(name):
        return 0.0

    if not ARTIST_INDEX_LOADED:
        load_artist_index()

    artist_count = _get_artist_count(name)
    if artist_count is None:
        # Unseen artist name: treat as maximally rare.
        return 1.0

    return _artist_score(ARTIST_INDEX_TOTAL_DOCS, artist_count)


def _mean_confidence_for_field(value: Any) -> float:
    names = _extract_names(value)
    if not names:
        return 0.0

    values = [_artist_confidence(name) for name in names]
    return sum(values) / len(values)

# return mean confidence between lost and auction artist-name sets.
def confidence_score(lost_artist_names: Any, auction_artist_names: Any) -> float:
    lost_names = _extract_names(lost_artist_names)
    auction_names = _extract_names(auction_artist_names)

    # returns 0.0 if either side has no valid artist name. This avoids assigning
    # partial confidence to records where artist metadata is missing on one side.
    if not lost_names or not auction_names:
        return 0.0

    lost_conf = sum(_artist_confidence(name) for name in lost_names) / len(lost_names)
    auction_conf = sum(_artist_confidence(name) for name in auction_names) / len( auction_names)

    return max(0.0, min(1.0, float((lost_conf + auction_conf) / 2.0)))


def confidence_function(lost_artist_names: Any, auction_artist_names: Any) -> float:
    return confidence_score(lost_artist_names, auction_artist_names)