# run with: python -m pytest -q tests/extraction_and_normalization
from __future__ import annotations

import pytest

from matching_pipeline.metadata_normalization.artist_normalization.normalize_names import (
    _normalize,
    _parse_parens_content,
    _smart_case,
    _strip_honorifics,
)


@pytest.mark.parametrize(
    "raw,expected_name",
    [
        # simple firstname lastname with parens stripped
        ("Max Liebermann (1847 Berlin - 1935 Berlin)", "Max Liebermann"),
        ("Lovis Corinth (1858 Tapiau - 1925 Zandvoort)", "Lovis Corinth"),
        ("Franz Marc (1880 München - 1916 Verdun)", "Franz Marc"),
        ("Max Beckmann (1884 Leipzig – 1950 New York)", "Max Beckmann"),
        # inverted LAST, First → re-ordered and title-cased
        ("BAISCH, Hermann (1846–1894)", "Hermann Baisch"),
        ("KOLLWITZ, Käthe (1867-1945)", "Käthe Kollwitz"),
        # attribution prefixes stripped, name normalized
        ("Attributed to Jan van Goyen (1596 Leiden - 1656 Den Haag)", "Jan van Goyen"),
        # "wohl" prefix stripped
        ("Wohl Emil Nolde (1867-1956)", "Emil Nolde"),
    ],
)
def test_normalize_name(raw, expected_name):
    result = _normalize("test_id", raw)
    assert result is not None
    assert result["matched"] is True
    assert result["artist_full_name"] == expected_name


@pytest.mark.parametrize(
    "raw",
    [
        "Unbekannter Künstler, süddeutsch",
        "Unbekannt, deutsch",
        "Unknown Artist",
        "Unbekannt",
        "Anonyme",
    ],
)
def test_normalize_unknown_artist_returns_null_name(raw):
    result = _normalize("test_id", raw)
    assert result is not None
    assert result["artist_full_name"] is None


@pytest.mark.parametrize(
    "raw,expected_prefix",
    [
        ("Circle of Peter Paul Rubens", "Circle of"),
        ("Follower of Rembrandt van Rijn", "Follower of"),
        ("Workshop of Lucas Cranach d.Ä.", "Workshop of"),
    ],
)
def test_normalize_attribution_qualifier_preserved(raw, expected_prefix):
    result = _normalize("test_id", raw)
    assert result is not None
    assert result["matched"] is True
    assert result["artist_full_name"].startswith(expected_prefix)


def test_normalize_extracts_birth_death_years():
    result = _normalize("test_id", "Max Liebermann (1847 Berlin - 1935 Berlin)")
    assert result is not None
    assert result["date_of_birth_raw_data"] == "1847"
    assert result["date_of_death_raw_data"] == "1935"


def test_normalize_extracts_birth_death_places():
    result = _normalize("test_id", "Max Liebermann (1847 Berlin - 1935 Berlin)")
    assert result is not None
    assert result["place_of_birth_raw_data"] == "Berlin"
    assert result["place_of_death_raw_data"] == "Berlin"


@pytest.mark.parametrize(
    "raw,expected_dob,expected_dod,expected_pob,expected_pod",
    [
        ("1847 Berlin - 1935 Berlin", "1847", "1935", "Berlin", "Berlin"),
        ("1858 Tapiau - 1925 Zandvoort", "1858", "1925", "Tapiau", "Zandvoort"),
        ("1858-1925", "1858", "1925", None, None),
        ("1880 München - 1916 Verdun", "1880", "1916", "München", "Verdun"),
        ("1847-", "1847", None, None, None),
    ],
)
def test_parse_parens_content(raw, expected_dob, expected_dod, expected_pob, expected_pod):
    result = _parse_parens_content(raw)
    assert result is not None
    assert result["dob"] == expected_dob
    assert result["dod"] == expected_dod
    assert result["pob"] == expected_pob
    assert result["pod"] == expected_pod


def test_parse_parens_content_ignorable_returns_empty():
    result = _parse_parens_content("?")
    assert result is not None
    assert result["dob"] is None
    assert result["dod"] is None


# ---------------------------------------------------------------------------
# _smart_case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("max liebermann", "Max Liebermann"),
        ("BAISCH", "Baisch"),
        ("mcdonald", "McDonald"),
        ("macgregor", "MacGregor"),
        ("Jan van Goyen", "Jan van Goyen"),  # "van" stays lowercase after first word
        ("circle of rembrandt", "Circle of Rembrandt"),  # "of" lowercase mid-string
    ],
)
def test_smart_case(raw, expected):
    assert _smart_case(raw) == expected


# ---------------------------------------------------------------------------
# _strip_honorifics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Smith, Jr.", "Smith"),
        ("Turner R.A.", "Turner"),
        ("Jones O.B.E.", "Jones"),
        ("Max Liebermann", "Max Liebermann"),  # no change
    ],
)
def test_strip_honorifics(raw, expected):
    assert _strip_honorifics(raw) == expected


# ---------------------------------------------------------------------------
# _normalize unmatched path
# ---------------------------------------------------------------------------


def test_normalize_returns_unmatched_for_multi_artist_entry():
    # "und" blocks _is_simple_name but isn't a disqualifier in the _NON_NAME_CORE check,
    # so the entry reaches the _unmatched path
    result = _normalize("test_id", "Müller und Meier")
    assert result is not None
    assert result.get("matched") is False
