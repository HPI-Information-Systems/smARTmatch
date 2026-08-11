"""Tests for image-matching persistence configuration."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from matching_pipeline.image_matching import config


class MatchingConfigPersistenceTests(unittest.TestCase):
    def test_csv_path_is_disabled_when_environment_value_is_missing(self) -> None:
        with (
            patch.object(config, "env_str", return_value=None),
            patch.object(config, "env_cache_dir") as cache_dir,
        ):
            self.assertIsNone(config.matching_results_csv_path_from_env())
        cache_dir.assert_not_called()

    def test_csv_path_requires_exact_opt_in_value(self) -> None:
        for value in ("0", "true", " 1", "yes"):
            with self.subTest(value=value), patch.object(
                config, "env_str", return_value=value
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "MATCHING_WRITE_OUTPUT_CSV must be unset or exactly '1'",
                ):
                    config.matching_results_csv_path_from_env()

    def test_csv_path_uses_configured_cache_directory(self) -> None:
        cache_dir = Path("/tmp/offline-matching-cache")
        with (
            patch.object(config, "env_str", return_value="1"),
            patch.object(config, "env_cache_dir", return_value=cache_dir),
        ):
            self.assertEqual(
                config.matching_results_csv_path_from_env(),
                cache_dir / "matching_results.csv",
            )


if __name__ == "__main__":
    unittest.main()
