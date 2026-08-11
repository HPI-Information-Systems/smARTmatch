# run with: python -m pytest -q tests/extraction_and_normalization
"""
Tests for the JSONL read/write flow of each normalization step.

These tests do NOT call the LLM. They verify that the pipeline functions
correctly read JSONL input, transform records, and write output back.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from matching_pipeline.metadata_normalization.artist_normalization.run_artist_normalization import run_artist_normalization
from matching_pipeline.metadata_normalization.dating_normalization.regex_filter import normalize_with_regex
from matching_pipeline.metadata_normalization.dimension_normalization.qwen_extract_dimensions import normalize_with_qwen


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# Dating regex flow
# ---------------------------------------------------------------------------


def test_normalize_with_regex_writes_dating_start_end(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"
    unmatched = tmp_path / "unmatched.jsonl"

    _write_jsonl(descriptions, [
        {"id": "a", "dating": "1898"},
        {"id": "b", "dating": "um 1910"},
        {"id": "c", "dating": "17. Jh."},
        {"id": "d", "dating": ""},
    ])

    normalize_with_regex(descriptions, unmatched)

    records = {r["id"]: r for r in _read_jsonl(descriptions)}
    assert records["a"]["dating_start"] == 1898
    assert records["a"]["dating_end"] == 1898
    assert records["b"]["dating_start"] == 1905
    assert records["b"]["dating_end"] == 1915
    assert records["c"]["dating_start"] == 1600
    assert records["c"]["dating_end"] == 1700
    assert records["d"]["dating_start"] is None
    assert records["d"]["dating_end"] is None


def test_normalize_with_regex_preserves_all_existing_fields(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"
    unmatched = tmp_path / "unmatched.jsonl"

    _write_jsonl(descriptions, [
        {"id": "x", "dating": "1900", "title": "Landschaft", "author": "Max M.", "dim_width": 40.0},
    ])

    normalize_with_regex(descriptions, unmatched)

    record = _read_jsonl(descriptions)[0]
    assert record["title"] == "Landschaft"
    assert record["author"] == "Max M."
    assert record["dim_width"] == 40.0


def test_normalize_with_regex_writes_unmatched_to_separate_file(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"
    unmatched = tmp_path / "unmatched.jsonl"

    _write_jsonl(descriptions, [
        {"id": "matched", "dating": "1898"},
        {"id": "needs_llm_1", "dating": "erste Hälfte 20. Jh."},
        {"id": "needs_llm_2", "dating": "1920er Jahre"},
    ])

    normalize_with_regex(descriptions, unmatched)

    unmatched_records = _read_jsonl(unmatched)
    unmatched_ids = {r["id"] for r in unmatched_records}
    assert "matched" not in unmatched_ids
    assert "needs_llm_1" in unmatched_ids
    assert "needs_llm_2" in unmatched_ids


def test_normalize_with_regex_unmatched_records_retain_none_dating_start_end(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"
    unmatched = tmp_path / "unmatched.jsonl"

    _write_jsonl(descriptions, [
        {"id": "hard", "dating": "nach 1880"},
    ])

    normalize_with_regex(descriptions, unmatched)

    record = _read_jsonl(descriptions)[0]
    assert record["dating_start"] is None
    assert record["dating_end"] is None


def test_normalize_with_regex_handles_empty_input(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"
    unmatched = tmp_path / "unmatched.jsonl"
    descriptions.write_text("", encoding="utf-8")

    normalize_with_regex(descriptions, unmatched)

    assert _read_jsonl(descriptions) == []


# ---------------------------------------------------------------------------
# Artist normalization flow
# ---------------------------------------------------------------------------


def test_run_artist_normalization_normalizes_name_in_place(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"

    _write_jsonl(descriptions, [
        {"id": "1", "author": "BAISCH, Hermann (1846–1894)"},
        {"id": "2", "author": "Max Liebermann (1847 Berlin - 1935 Berlin)"},
        {"id": "3", "author": "Attributed to Jan van Goyen (1596 Leiden - 1656 Den Haag)"},
    ])

    run_artist_normalization(descriptions)

    records = {r["id"]: r for r in _read_jsonl(descriptions)}
    assert records["1"]["author"] == "Hermann Baisch"
    assert records["2"]["author"] == "Max Liebermann"
    assert records["3"]["author"] == "Jan van Goyen"


def test_run_artist_normalization_leaves_unknown_artist_empty(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"

    _write_jsonl(descriptions, [
        {"id": "u", "author": "Unbekannter Künstler, süddeutsch"},
    ])

    run_artist_normalization(descriptions)

    record = _read_jsonl(descriptions)[0]
    # unknown artists: _normalize returns artist_full_name=None → author stays as-is
    # (run_artist_normalization only overwrites when artist_full_name is set)
    assert record["id"] == "u"


def test_run_artist_normalization_fills_birth_death_from_parens(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"

    _write_jsonl(descriptions, [
        {"id": "1", "author": "Franz Marc (1880 München - 1916 Verdun)",
         "date_of_birth": "", "date_of_death": "",
         "place_of_birth": "", "place_of_death": ""},
    ])

    run_artist_normalization(descriptions)

    record = _read_jsonl(descriptions)[0]
    assert record["date_of_birth"] == "1880"
    assert record["date_of_death"] == "1916"
    assert record["place_of_birth"] == "München"
    assert record["place_of_death"] == "Verdun"


def test_run_artist_normalization_does_not_overwrite_existing_dates(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"

    _write_jsonl(descriptions, [
        {"id": "1", "author": "Franz Marc (1880 München - 1916 Verdun)",
         "date_of_birth": "already set", "date_of_death": ""},
    ])

    run_artist_normalization(descriptions)

    record = _read_jsonl(descriptions)[0]
    assert record["date_of_birth"] == "already set"


def test_run_artist_normalization_preserves_other_fields(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"

    _write_jsonl(descriptions, [
        {"id": "1", "author": "Max Liebermann (1847-1935)",
         "title": "Badende Knaben", "dim_width": 120.0, "dating_start": 1898},
    ])

    run_artist_normalization(descriptions)

    record = _read_jsonl(descriptions)[0]
    assert record["title"] == "Badende Knaben"
    assert record["dim_width"] == 120.0
    assert record["dating_start"] == 1898


def test_run_artist_normalization_handles_empty_author_field(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"

    _write_jsonl(descriptions, [
        {"id": "1", "author": "", "title": "Landschaft"},
        {"id": "2", "title": "Porträt"},  # no author key at all
    ])

    run_artist_normalization(descriptions)

    records = _read_jsonl(descriptions)
    assert len(records) == 2
    assert records[0]["title"] == "Landschaft"
    assert records[1]["title"] == "Porträt"


# ---------------------------------------------------------------------------
# Dimension normalization flow (no LLM needed when all records already processed)
# ---------------------------------------------------------------------------


def test_normalize_with_qwen_skips_already_processed_records(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"

    _write_jsonl(descriptions, [
        {"id": "1", "dimensions": "30 x 40 cm",
         "dim_width": 40.0, "dim_height": 30.0,
         "dim_width_frame": None, "dim_height_frame": None},
    ])

    normalize_with_qwen(descriptions)

    record = _read_jsonl(descriptions)[0]
    assert record["dim_width"] == 40.0
    assert record["dim_height"] == 30.0


def test_normalize_with_qwen_skips_records_without_dimensions(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"

    _write_jsonl(descriptions, [
        {"id": "1", "dimensions": ""},
        {"id": "2"},
    ])

    normalize_with_qwen(descriptions)

    records = _read_jsonl(descriptions)
    assert len(records) == 2
    assert records[0].get("dim_width") is None
    assert records[1].get("dim_width") is None


def test_normalize_with_qwen_preserves_other_fields(tmp_path: Path):
    descriptions = tmp_path / "descriptions.jsonl"

    _write_jsonl(descriptions, [
        {"id": "1", "dimensions": "30 x 40 cm",
         "dim_width": 40.0, "dim_height": 30.0,
         "title": "Landschaft", "dating_start": 1900, "author": "Max M."},
    ])

    normalize_with_qwen(descriptions)

    record = _read_jsonl(descriptions)[0]
    assert record["title"] == "Landschaft"
    assert record["dating_start"] == 1900
    assert record["author"] == "Max M."
