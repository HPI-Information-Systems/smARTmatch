"""Tests for the independently scheduled metadata stage entrypoints."""

from __future__ import annotations

from unittest import mock

from matching_pipeline.metadata_extraction import __main__ as extraction_stage
from matching_pipeline.metadata_matching import run_metadata_matching as matching_stage


def test_extraction_stage_skips_when_no_artwork_is_eligible():
    with mock.patch.object(
        extraction_stage, "_has_eligible_artworks", return_value=False
    ), mock.patch.object(
        extraction_stage, "run_extraction_normalization"
    ) as run_pipeline:
        assert extraction_stage.main() == 0

    run_pipeline.assert_not_called()


def test_matching_stage_skips_when_no_artwork_is_eligible():
    with mock.patch.object(
        matching_stage, "_has_eligible_artworks", return_value=False
    ):
        result = matching_stage.run_metadata_matching()

    assert result == {
        "lost_loaded": 0,
        "auction_pairs_processed": 0,
        "elapsed_seconds": 0.0,
    }
