"""Tests for the file-backed LightGlue matching runner."""

from __future__ import annotations

import csv
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.modules.setdefault(
    "dotenv",
    types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: False),
)


class _FakeFeatureExtractor:
    device = "fake-cpu"

    def extract_prepared(self, prepared) -> dict[str, object]:
        return {"image_path": str(prepared.path), "keypoints": [[10.0, 20.0]]}

    def load_or_extract(
        self,
        feats_path: Path,
        image_path: Path,
        save_missing_feats: bool = True,
        prepared_image=None,
    ) -> dict[str, object]:
        return {"image_path": str(image_path), "keypoints": [[30.0, 40.0]]}


class _FakeFeatureMatcher:
    device = "fake-cpu"

    def match(self, feats0: dict[str, object], feats1: dict[str, object]) -> dict[str, object]:
        return {"matches": [[0, 0]], "scores": [0.9]}


class _FakeMatchClassifier:
    def classify_matches(self, matches: dict[str, object]) -> tuple[bool, float]:
        return True, 0.75


def _load_runner_module():
    fake_models = types.ModuleType("matching_pipeline.image_matching.models")
    fake_models.DEFAULT_IMAGE_RESIZE = 720
    fake_models.PreparedImage = object
    fake_models.configure_parallel_image_resize = lambda: None
    fake_models.prepare_image = lambda path, *, resize: types.SimpleNamespace(
        path=Path(path),
        resize=resize,
    )
    fake_models.FeatureExtractor = _FakeFeatureExtractor
    fake_models.FeatureMatcher = _FakeFeatureMatcher
    fake_models.MatchClassifier = _FakeMatchClassifier
    sys.modules["matching_pipeline.image_matching.models"] = fake_models
    module = importlib.import_module("matching_pipeline.image_matching.run_image_matching")
    return importlib.reload(module)


class RunImageMatchingTests(unittest.TestCase):
    def test_matching_results_csv_includes_joined_image_paths(self) -> None:
        runner = _load_runner_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            results_csv = root / "matching_results.csv"
            auction_path = root / "auction.jpg"
            lost_path = root / "lost.jpg"
            items = [
                {
                    "auction_file_id": "auction-1",
                    "auction_file_path": str(auction_path),
                    "auction_content_version": 2,
                    "lost_content_revision": 7,
                    "match_candidates": [
                        {
                            "lost_file_id": "lost-1",
                            "lost_file_path": str(lost_path),
                            "blocking_score": 0.5,
                        }
                    ],
                }
            ]

            with mock.patch.object(
                runner,
                "summarize_auction_to_lost_rankings",
                return_value={"part_count": 1, "row_count": 1, "auction_file_count": 1},
            ), mock.patch.object(
                runner,
                "load_auction_to_lost_rankings_with_paths",
                return_value=iter(items),
            ):
                result = runner.run_image_matching(results_csv=results_csv, feats_dir=None)

            self.assertEqual(result.processed_auction_file_ids, ["auction-1"])
            self.assertEqual(result.lost_content_revision, 7)
            self.assertEqual(result.auction_content_versions, {"auction-1": 2})
            self.assertEqual(len(result.accepted_matches), 1)
            self.assertEqual(result.pairs_processed, 1)
            with results_csv.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(
                    reader.fieldnames,
                    [
                        "auction_file_id",
                        "auction_file_path",
                        "lost_file_id",
                        "lost_file_path",
                        "confidence",
                        "blocking_score",
                    ],
                )
                self.assertEqual(
                    list(reader),
                    [
                        {
                            "auction_file_id": "auction-1",
                            "auction_file_path": str(auction_path),
                            "lost_file_id": "lost-1",
                            "lost_file_path": str(lost_path),
                            "confidence": "0.75",
                            "blocking_score": "0.5",
                        }
                    ],
                )

    def test_no_candidates_skips_model_initialization(self) -> None:
        runner = _load_runner_module()
        with tempfile.TemporaryDirectory() as tmp_dir:
            results_csv = Path(tmp_dir) / "matching_results.csv"
            with mock.patch.object(
                runner,
                "summarize_auction_to_lost_rankings",
                return_value={"part_count": 0, "row_count": 0, "auction_file_count": 0},
            ), mock.patch.object(
                runner,
                "load_auction_to_lost_rankings_with_paths",
                side_effect=AssertionError("rankings should not be loaded"),
            ), mock.patch.object(
                runner,
                "FeatureExtractor",
                side_effect=AssertionError("extractor should not be initialized"),
            ), mock.patch.object(
                runner,
                "FeatureMatcher",
                side_effect=AssertionError("matcher should not be initialized"),
            ), mock.patch.object(
                runner,
                "MatchClassifier",
                side_effect=AssertionError("classifier should not be initialized"),
            ):
                result = runner.run_image_matching(results_csv=results_csv, feats_dir=None)

            self.assertEqual(result.processed_auction_file_ids, [])
            self.assertEqual(result.accepted_matches, [])
            self.assertEqual(result.pairs_processed, 0)
            self.assertFalse(results_csv.exists())


if __name__ == "__main__":
    unittest.main()
