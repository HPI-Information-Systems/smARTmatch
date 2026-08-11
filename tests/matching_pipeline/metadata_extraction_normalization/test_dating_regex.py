# run with: python -m pytest -q tests/extraction_and_normalization
from __future__ import annotations

import pytest

from matching_pipeline.metadata_normalization.dating_normalization.regex_filter import parse_dating_range


@pytest.mark.parametrize(
    "dating,expected_start,expected_end",
    [
        # exact year
        ("1898", 1898, 1898),
        ("1903", 1903, 1903),
        ("1922", 1922, 1922),
        # approximate single year
        ("um 1910", 1905, 1915),
        ("ca. 1850", 1845, 1855),
        ("circa 1780", 1775, 1785),
        # dat./dated prefix
        ("dat. 1645", 1645, 1645),
        ("datiert 1770", 1770, 1770),
        ("dated 1900", 1900, 1900),
        # full YYYY-YYYY interval
        ("1911-1912", 1911, 1912),
        ("1620-1640", 1620, 1640),
        # short second year: YYYY-YY
        ("1950-55", 1950, 1955),
        ("1780-90", 1780, 1790),
        # German century
        ("17. Jh.", 1600, 1700),
        ("19. Jh.", 1800, 1900),
        ("19. Jahrhundert", 1800, 1900),
        # English century
        ("17th century", 1600, 1700),
        ("19th century", 1800, 1900),
        # uncertain year (with ?)
        ("1850 (?)", 1850, 1850),
    ],
)
def test_parse_dating_range_matched(dating, expected_start, expected_end):
    (start, end), matched = parse_dating_range(dating)
    assert matched, f"Expected {dating!r} to be matched by regex"
    assert start == expected_start
    assert end == expected_end


@pytest.mark.parametrize(
    "dating",
    [
        # complex patterns that require the LLM fallback
        "erste Hälfte 20. Jh.",
        "2. Hälfte 19. Jahrhundert",
        "1. Hälfte 16. Jahrhundert",
        "1920er Jahre",
        "nach 1880",
        "vor 1900",
        "Ende 19./Anfang 20. Jh.",
        "ca. 1620-1640",
    ],
)
def test_parse_dating_range_unmatched(dating):
    (start, end), matched = parse_dating_range(dating)
    assert not matched, f"Expected {dating!r} to be unmatched (needs LLM)"
    assert start is None
    assert end is None


def test_parse_dating_range_empty_string():
    (start, end), matched = parse_dating_range("")
    assert not matched
    assert (start, end) == (None, None)


def test_parse_dating_range_whitespace_only():
    (start, end), matched = parse_dating_range("   ")
    assert not matched
    assert (start, end) == (None, None)
