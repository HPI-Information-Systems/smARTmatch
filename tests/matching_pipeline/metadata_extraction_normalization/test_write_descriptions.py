# run with: python -m pytest -q tests/extraction_and_normalization
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from matching_pipeline.metadata_extraction import qwen_extract_information as extraction
from matching_pipeline.metadata_extraction.status import EXTRACTION_PARSED_FIELD
from matching_pipeline.metadata_normalization import write_descriptions as writer
from matching_pipeline.metadata_normalization.write_descriptions import (
    _f,
    _has_metadata_payload,
    _i,
    _s,
)


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


# ---------------------------------------------------------------------------
# Per-record extraction status and persistence
# ---------------------------------------------------------------------------


def test_raw_output_parser_distinguishes_valid_empty_schema_from_malformed():
    valid_empty = json.dumps({field: "" for field in extraction.OUTPUT_FIELDS})
    entities, parsed = extraction._parse_entities_from_raw_output(valid_empty)
    assert parsed is True
    assert all(not entities[field] for field in extraction.OUTPUT_FIELDS)

    invalid_outputs = [
        "not JSON",
        json.dumps({"title": ""}),
        json.dumps(
            {
                **{field: "" for field in extraction.OUTPUT_FIELDS},
                "title": [],
            }
        ),
        valid_empty + "\n{malformed final answer",
    ]
    for raw in invalid_outputs:
        entities, parsed = extraction._parse_entities_from_raw_output(raw)
        assert parsed is False
        assert all(not entities[field] for field in extraction.OUTPUT_FIELDS)

    fenced_entities, fenced_parsed = extraction._parse_entities_from_raw_output(
        f"```json\n{valid_empty}\n```"
    )
    assert fenced_parsed is True
    assert all(not fenced_entities[field] for field in extraction.OUTPUT_FIELDS)


def test_extract_metadata_marks_each_record_parse_status(tmp_path):
    descriptions = tmp_path / "descriptions.jsonl"
    input_records = [
        {"id": "populated", "description": "title"},
        {"id": "valid-empty", "description": "nothing extractable"},
        {"id": "malformed", "description": "bad response"},
    ]
    descriptions.write_text(
        "".join(json.dumps(record) + "\n" for record in input_records),
        encoding="utf-8",
    )
    valid_empty = json.dumps({field: "" for field in extraction.OUTPUT_FIELDS})
    config = SimpleNamespace(
        backend="transformers",
        model="test/model",
        quantization=None,
        device="cpu",
        gpu_memory_utilization=0.5,
        max_num_seqs=1,
    )

    populated = {field: "" for field in extraction.OUTPUT_FIELDS}
    populated["title"] = "A"
    with mock.patch.object(
        extraction, "get_model_config", return_value=config
    ), mock.patch.object(
        extraction,
        "_run_transformers",
        return_value=[json.dumps(populated), valid_empty, "not JSON"],
    ):
        extraction.extract_metadata(descriptions, backend="transformers")

    output_records = [
        json.loads(line) for line in descriptions.read_text(encoding="utf-8").splitlines()
    ]
    assert [record[EXTRACTION_PARSED_FIELD] for record in output_records] == [
        True,
        True,
        False,
    ]
    assert output_records[0]["title"] == "A"
    assert output_records[1]["title"] == ""
    assert output_records[2]["title"] == ""


def test_write_descriptions_leaves_unparseable_records_pending(tmp_path, caplog):
    descriptions = tmp_path / "descriptions.jsonl"
    records = [
        {"id": "payload", "title": "Parsed legacy payload"},
        {"id": "valid-empty", EXTRACTION_PARSED_FIELD: True},
        {"id": "malformed", EXTRACTION_PARSED_FIELD: False},
        {"id": "legacy-empty"},
        {
            "id": "failed-with-payload",
            "title": "must not leak",
            EXTRACTION_PARSED_FIELD: False,
        },
    ]
    descriptions.write_text(
        "".join(json.dumps(record) + "\n" for record in records) + "not-json\n",
        encoding="utf-8",
    )

    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    connection = mock.MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value = cursor

    with mock.patch.object(writer, "db_connect", return_value=connection):
        writer.write_descriptions(descriptions)

    cursor.executemany.assert_called_once()
    persisted_rows = cursor.executemany.call_args.args[1]
    assert [row[-1] for row in persisted_rows] == ["payload"]
    cursor.execute.assert_called_once_with(
        "UPDATE auction_artwork SET is_metadata_extraction_processed = true WHERE auction_artwork_id = ANY(%s)",
        (["valid-empty"],),
    )
    connection.commit.assert_called_once_with()
    assert "Left 4 metadata extraction records pending for retry" in caplog.text
