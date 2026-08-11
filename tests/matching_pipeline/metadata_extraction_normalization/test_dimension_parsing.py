# run with: python -m pytest -q tests/extraction_and_normalization
from __future__ import annotations

import pytest

from matching_pipeline.metadata_normalization.dimension_normalization.qwen_extract_dimensions import (
    _extract_json_objects,
    _parse_dimensions,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            '{"width": 60.0, "height": 75.5, "width_frame": 79.0, "height_frame": 95.0}',
            {"width": 60.0, "height": 75.5, "width_frame": 79.0, "height_frame": 95.0},
        ),
        (
            '{"width": 50.0, "height": 66.0, "width_frame": null, "height_frame": null}',
            {"width": 50.0, "height": 66.0, "width_frame": None, "height_frame": None},
        ),
        # circular: diameter in both width and height
        (
            '{"width": 28.0, "height": 28.0, "width_frame": null, "height_frame": null}',
            {"width": 28.0, "height": 28.0, "width_frame": None, "height_frame": None},
        ),
        # height only
        (
            '{"width": null, "height": 92.0, "width_frame": null, "height_frame": null}',
            {"width": None, "height": 92.0, "width_frame": None, "height_frame": None},
        ),
    ],
)
def test_parse_dimensions_clean_json(raw, expected):
    result = _parse_dimensions(raw)
    assert result == expected


def test_parse_dimensions_extracts_from_surrounding_text():
    raw = (
        'Here is the result: '
        '{"width": 41.3, "height": 29.8, "width_frame": null, "height_frame": null}'
    )
    result = _parse_dimensions(raw)
    assert result is not None
    assert result["width"] == pytest.approx(41.3)
    assert result["height"] == pytest.approx(29.8)
    assert result["width_frame"] is None


def test_parse_dimensions_returns_none_on_no_json():
    assert _parse_dimensions("keine Abmessungen gefunden") is None
    assert _parse_dimensions("") is None


def test_parse_dimensions_prefers_object_with_more_populated_fields():
    # first object has no populated fields, second has two
    raw = (
        '{"width": null, "height": null, "width_frame": null, "height_frame": null} '
        '{"width": 50.0, "height": 66.0, "width_frame": null, "height_frame": null}'
    )
    result = _parse_dimensions(raw)
    assert result is not None
    assert result["width"] == pytest.approx(50.0)
    assert result["height"] == pytest.approx(66.0)


def test_parse_dimensions_ignores_object_without_dimension_fields():
    raw = '{"some_other_key": 1} {"width": 45.0, "height": 34.0, "width_frame": 63.0, "height_frame": 52.0}'
    result = _parse_dimensions(raw)
    assert result is not None
    assert result["width"] == pytest.approx(45.0)
    assert result["width_frame"] == pytest.approx(63.0)


@pytest.mark.parametrize(
    "raw,expected_count",
    [
        ('{"width": 1.0}', 1),
        ('{"a": 1} some text {"b": 2}', 2),
        ("no json here", 0),
        ("", 0),
        ("{invalid}", 0),
    ],
)
def test_extract_json_objects_count(raw, expected_count):
    result = _extract_json_objects(raw)
    assert len(result) == expected_count


def test_extract_json_objects_returns_dicts():
    result = _extract_json_objects('{"width": 60.0, "height": 75.5}')
    assert len(result) == 1
    assert result[0]["width"] == 60.0
    assert result[0]["height"] == 75.5
