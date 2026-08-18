"""Unit coverage for time-based image-blocking progress logs."""

from __future__ import annotations

import unittest
from threading import Event
from unittest import mock

from matching_pipeline.image_blocking import progress


class ImageProgressTests(unittest.TestCase):
    def test_logs_on_timer_and_always_logs_final_throughput(self) -> None:
        heartbeat_logged = Event()

        def capture(message, *_args) -> None:
            if "Blocking progress" in message:
                heartbeat_logged.set()

        with mock.patch.object(progress.logger, "info", side_effect=capture) as info:
            with progress.ImageProgress(
                "lost_embeddings",
                5,
                interval_seconds=0.01,
            ) as tracker:
                tracker.update(4)
                self.assertTrue(heartbeat_logged.wait(1.0))
                tracker.update(5)

        periodic = [
            call for call in info.call_args_list if "Blocking progress" in call.args[0]
        ]
        final = [
            call
            for call in info.call_args_list
            if "Blocking stage finished" in call.args[0]
        ]
        self.assertTrue(periodic)
        self.assertTrue(
            any(call.args[1:4] == ("lost_embeddings", 4, 5) for call in periodic)
        )
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0].args[1:4], ("lost_embeddings", 5, 5))
        self.assertGreater(final[0].args[4], 0.0)

    def test_progress_validation_and_formatting_helpers(self) -> None:
        self.assertEqual(progress.PROGRESS_INTERVAL_SECONDS, 20.0)
        with self.assertRaisesRegex(ValueError, "total_images"):
            progress.ImageProgress("stage", -1)
        with self.assertRaisesRegex(ValueError, "interval_seconds"):
            progress.ImageProgress("stage", 1, interval_seconds=0)
        with progress.ImageProgress("stage", 1) as tracker:
            with self.assertRaisesRegex(ValueError, "completed_images"):
                tracker.update(2)

        self.assertEqual(progress._throughput(0, 2.0), 0.0)
        self.assertEqual(progress._throughput(4, 2.0), 2.0)
        self.assertEqual(progress._eta_text(10, 0, 2.0), "unknown")
        self.assertEqual(progress._eta_text(10, 4, 2.0), "3.0s")
        self.assertEqual(progress._format_duration(65), "1m05s")
        self.assertEqual(progress._format_duration(3661), "1h01m01s")


if __name__ == "__main__":
    unittest.main()
