"""Integration test for the real blocking-to-matching artifact boundary."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from matching_pipeline.image_blocking.candidate_generation import write_candidate_parts
from matching_pipeline.image_blocking.input_sources import ImageFileRow
from matching_pipeline.image_matching import run_image_matching as matching_runtime
from matching_pipeline.shared.artifacts import write_image_files_parquet


class _EmbeddingModel:
    def generate_embeddings_batch(self, paths):
        return np.asarray([[1.0, 0.0] for _path in paths], dtype=np.float32)


class _FeatureExtractor:
    device = "cpu"

    def extract(self, _path):
        return {"features": "auction"}

    def load_or_extract(self, _features_path, _image_path, *, save_missing_feats):
        return {"features": "lost", "saved": save_missing_feats}


class _FeatureMatcher:
    device = "cpu"

    def match(self, auction_features, lost_features):
        return {"auction": auction_features, "lost": lost_features}


class _Classifier:
    def classify_matches(self, _matches):
        return True, 0.95


class PipelineArtifactIntegrationTests(unittest.TestCase):
    def test_real_parquet_handoff_reaches_matching_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_root = root / "images"
            cache_root = root / "cache"
            image_root.mkdir()
            lost_path = image_root / "lost.jpg"
            auction_path = image_root / "auction.jpg"
            lost_path.write_bytes(b"lost")
            auction_path.write_bytes(b"auction")
            env = {
                "CACHE_DIR": str(cache_root),
                "SMARTMATCH_IMAGES_DIR": str(image_root),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                write_image_files_parquet(
                    "lost", [ImageFileRow("lost-1", lost_path)]
                )
                write_image_files_parquet(
                    "auction", [ImageFileRow("auction-1", auction_path)]
                )
                candidate_count, part_count, skipped_count = write_candidate_parts(
                    [
                        ImageFileRow(
                            "auction-1", auction_path, content_version=2
                        )
                    ],
                    ["lost-1"],
                    np.asarray([[1.0, 0.0]], dtype=np.float32),
                    lambda: _EmbeddingModel(),
                    model_identity="integration/dino-model",
                    lost_source_identity="integration-lost-source-v1",
                    lost_content_versions={"lost-1": 1},
                    lost_content_sha256={
                        "lost-1": hashlib.sha256(b"lost").hexdigest()
                    },
                    top_k=1,
                    image_batch_size=1,
                    shard_size=1,
                    lost_content_revision=7,
                )
                results_csv = root / "results.csv"
                with mock.patch.object(
                    matching_runtime, "FeatureExtractor", _FeatureExtractor
                ), mock.patch.object(
                    matching_runtime, "FeatureMatcher", _FeatureMatcher
                ), mock.patch.object(
                    matching_runtime, "MatchClassifier", _Classifier
                ), mock.patch.object(
                    matching_runtime,
                    "build_keypoint_match_visualization",
                    return_value={"matches": [[0, 1]]},
                ):
                    result = matching_runtime.run_image_matching(
                        results_csv=results_csv,
                        feats_dir=None,
                    )
                results_csv_text = results_csv.read_text()

        self.assertEqual((candidate_count, part_count, skipped_count), (1, 1, 0))
        self.assertEqual(result.processed_auction_file_ids, ["auction-1"])
        self.assertEqual(result.lost_content_revision, 7)
        self.assertEqual(result.auction_content_versions, {"auction-1": 2})
        self.assertEqual(result.pairs_processed, 1)
        self.assertEqual(len(result.accepted_matches), 1)
        accepted = result.accepted_matches[0]
        self.assertEqual(accepted.lost_file_id, "lost-1")
        self.assertEqual(accepted.auction_file_path, str(auction_path))
        self.assertAlmostEqual(accepted.blocking_score, 1.0)
        self.assertIn("auction-1", results_csv_text)


if __name__ == "__main__":
    unittest.main()
