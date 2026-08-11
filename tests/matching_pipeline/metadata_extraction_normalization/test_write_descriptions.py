# run with: python -m pytest -q tests/extraction_and_normalization
from __future__ import annotations

import pytest

from matching_pipeline.metadata_normalization.write_descriptions import _f, _has_metadata_payload, _i, _s


# ---------------------------------------------------------------------------
# _s — string coercion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("val", [None, "", "   "])
def test_s_returns_none_for_empty(val):
    assert _s(val) is None


def test_s_strips_whitespace():
    assert _s("  Landschaft  ") == "Landschaft"


def test_s_converts_non_string():
    assert _s(1898) == "1898"


# ---------------------------------------------------------------------------
# _f — float coercion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "val,expected",
    [
        (30.0, 30.0),
        ("30.5", 30.5),
        (0, 0.0),
        (0.0, 0.0),
    ],
)
def test_f_converts_to_float(val, expected):
    assert _f(val) == pytest.approx(expected)


def test_f_returns_none_for_none():
    assert _f(None) is None


def test_f_returns_none_for_invalid():
    assert _f("kein Wert") is None


# ---------------------------------------------------------------------------
# _i — int coercion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "val,expected",
    [
        (1898, 1898),
        ("1900", 1900),
        (1900.7, 1900),
    ],
)
def test_i_converts_to_int(val, expected):
    assert _i(val) == expected


def test_i_returns_none_for_none():
    assert _i(None) is None


def test_i_returns_none_for_invalid():
    assert _i("nicht eine Zahl") is None


# ---------------------------------------------------------------------------
# _has_metadata_payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rec",
    [
        {"title": "Landschaft"},
        {"author": "Max Liebermann"},
        {"technique": "Öl", "title": ""},
    ],
)
def test_has_metadata_payload_true(rec):
    assert _has_metadata_payload(rec) is True


@pytest.mark.parametrize(
    "rec",
    [
        {},
        {"title": "", "author": None},
        {"dating_start": 1900},  # numeric field not in payload list → False
    ],
)
def test_has_metadata_payload_false(rec):
    assert _has_metadata_payload(rec) is False
