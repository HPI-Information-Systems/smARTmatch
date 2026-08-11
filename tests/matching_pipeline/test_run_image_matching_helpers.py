"""Unit coverage for matching runner progress and formatting helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from matching_pipeline.image_matching import run_image_matching as runtime


class MatchingRuntimeHelperTests(unittest.TestCase):
    def test_candidate_summary_and_result_writer(self) -> None:
        summary = {"part_count": 1, "row_count": 2, "auction_file_count": 1}
        with (
            mock.patch.object(
                runtime,
                "env_auction_to_lost_rankings_dir",
                return_value=Path("parts"),
            ),
            mock.patch.object(
                runtime,
                "summarize_auction_to_lost_rankings",
                return_value=summary,
            ) as summarize,
        ):
            self.assertIs(runtime._candidate_artifact_summary(), summary)
        summarize.assert_called_once_with()

        matches = [mock.Mock()]
        runtime._write_results(None, matches)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.csv"
            with (
                mock.patch.object(runtime, "save_matches_to_csv") as save,
                mock.patch.object(
                    runtime, "perf_counter", side_effect=[1.0, 2.25]
                ),
            ):
                runtime._write_results(output, matches)
        save.assert_called_once_with(matches, output)

    def test_progress_interval_average_and_eta_logging(self) -> None:
        cases = [
            ([10.0, 11.0], 2.0, 0, False),
            ([10.0, 13.0], 2.0, 0, True),
            ([20.0, 24.0], 0, 2, True),
        ]
        for times, interval, pairs, should_log in cases:
            with (
                self.subTest(times=times),
                mock.patch.object(runtime, "perf_counter", side_effect=times),
                mock.patch.object(runtime.logger, "info") as info,
            ):
                progress = runtime._MatchingProgress(
                    total_auctions=2,
                    total_pairs=4,
                    interval_seconds=interval,
                )
                progress.maybe_log(
                    auctions_processed=1,
                    pairs_processed=pairs,
                    matches_found=1,
                    failed_images=1,
                    failed_pairs=2,
                )
            self.assertEqual(info.called, should_log)
            if should_log:
                self.assertEqual(progress.last_logged_at, times[-1])

    def test_format_eta_count_and_path_helpers(self) -> None:
        self.assertEqual(runtime._eta_text(0, 1, 1.0), "unknown")
        self.assertEqual(runtime._eta_text(2, 0, 1.0), "unknown")
        self.assertEqual(runtime._eta_text(2, 1, None), "unknown")
        self.assertEqual(runtime._eta_text(5, 2, 2.0), "6.0s")
        self.assertEqual(runtime._eta_text(2, 5, 2.0), "0.0s")
        self.assertEqual(runtime._count_text(4), "4")
        self.assertEqual(runtime._count_text(0), "unknown")
        self.assertIsNone(runtime._display_path(None))
        absolute = Path.cwd().resolve()
        self.assertEqual(runtime._display_path(absolute), str(absolute))
        self.assertEqual(
            runtime._display_path(Path("relative")),
            str((Path.cwd() / "relative").resolve()),
        )
        self.assertEqual(runtime._format_duration(2.25), "2.2s")
        self.assertEqual(runtime._format_duration(65), "1m05s")
        self.assertEqual(runtime._format_duration(3661), "1h01m01s")


if __name__ == "__main__":
    unittest.main()
