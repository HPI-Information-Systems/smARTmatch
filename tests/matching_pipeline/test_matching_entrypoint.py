"""Tests for the standalone image-matching stage entrypoint."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from matching_pipeline.image_matching import __main__ as entrypoint


class ImageMatchingEntrypointTests(unittest.TestCase):
    def test_logging_configuration_is_explicit(self) -> None:
        with mock.patch.object(entrypoint.logging, "basicConfig") as basic_config:
            entrypoint._configure_logging()
        basic_config.assert_called_once_with(
            level=entrypoint.logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            force=True,
        )

    def test_upstream_failure_skips_matching(self) -> None:
        with mock.patch.dict(os.environ, {entrypoint._SKIP_ENV: "1"}, clear=False), mock.patch.object(
            entrypoint, "_configure_logging"
        ), mock.patch.object(entrypoint, "run_image_matching") as run_matching:
            self.assertEqual(entrypoint.main(), 0)
        run_matching.assert_not_called()

    def test_results_are_persisted(self) -> None:
        result = object()
        db_result = mock.Mock(
            match_score_count=3,
            processed_auction_link_count=2,
            processed_auction_artwork_count=1,
        )
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            entrypoint, "_configure_logging"
        ), mock.patch.object(
            entrypoint, "matching_results_csv_path_from_env", return_value=None
        ), mock.patch.object(
            entrypoint, "run_image_matching", return_value=result
        ) as run_matching, mock.patch.object(
            entrypoint, "write_matching_run_to_db", return_value=db_result
        ) as write_results:
            self.assertEqual(entrypoint.main(), 0)

        run_matching.assert_called_once_with(results_csv=None)
        write_results.assert_called_once_with(result)


if __name__ == "__main__":
    unittest.main()
