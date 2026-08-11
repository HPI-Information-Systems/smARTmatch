"""Unit tests for image-blocking configuration."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from matching_pipeline.image_blocking import config


class ConfigTests(unittest.TestCase):
    def test_paths_and_environment_configuration(self) -> None:
        root = Path("/tmp/cache")
        image_root = Path("/tmp/images")
        with (
            mock.patch.object(config, "env_image_root", return_value=image_root),
            mock.patch.object(config, "env_image_blocking_dir", return_value=root),
            mock.patch.object(
                config,
                "env_auction_to_lost_rankings_dir",
                return_value=root / "rankings",
            ),
        ):
            self.assertEqual(config.default_image_root(), image_root)
            self.assertEqual(config.blocking_root(), root)
            self.assertEqual(
                config.lost_embedding_cache_path(), root / "lost" / "embeddings.npz"
            )
            self.assertEqual(
                config.blocking_input_csv_path(), root / "blocking_input.csv"
            )
            self.assertEqual(config.candidate_dir(), root / "rankings")
        self.assertEqual(config.repo_root(), Path(config.__file__).resolve().parents[2])
        self.assertEqual(config.VALID_ROLES, {"lost", "auction"})

    def test_matching_batch_size_reads_and_validates_environment(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MATCHING_BATCH_SIZE", None)
            self.assertEqual(
                config.matching_batch_size_from_env(), config.DEFAULT_MATCHING_BATCH_SIZE
            )
        with mock.patch.dict(os.environ, {"MATCHING_BATCH_SIZE": "7"}):
            self.assertEqual(config.matching_batch_size_from_env(), 7)
        with mock.patch.dict(os.environ, {"MATCHING_BATCH_SIZE": "0"}):
            with self.assertRaisesRegex(ValueError, "positive integer"):
                config.matching_batch_size_from_env()
        with mock.patch.dict(os.environ, {"MATCHING_BATCH_SIZE": "invalid"}):
            with self.assertRaisesRegex(ValueError, "must be an integer"):
                config.matching_batch_size_from_env()


if __name__ == "__main__":
    unittest.main()
