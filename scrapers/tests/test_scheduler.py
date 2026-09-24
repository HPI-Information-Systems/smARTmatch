from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from shared.image_storage_lock import image_storage_lock

from scrapers.scheduler import (
    BatchProcessManager,
    SchedulerConfig,
    load_config,
    next_trigger_after,
    parse_interval,
    run_interval_cleanup,
    run_scheduler,
)


class SchedulerTests(unittest.TestCase):
    def test_load_config_uses_single_interval_setting(self) -> None:
        with patch.dict("os.environ", {"SCRAPER_INTERVAL": "1d"}, clear=True):
            config = load_config()

        self.assertEqual(config.interval_seconds, 86_400)

    def test_load_config_defaults_to_one_day(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            config = load_config()

        self.assertEqual(config.interval_seconds, 86_400)

    def test_parse_interval_supports_simple_units(self) -> None:
        self.assertEqual(parse_interval("30s"), 30)
        self.assertEqual(parse_interval("15m"), 900)
        self.assertEqual(parse_interval("12h"), 43_200)
        self.assertEqual(parse_interval(" 2D "), 172_800)

    def test_parse_interval_rejects_invalid_or_zero_values(self) -> None:
        for value in ("", "0d", "1", "daily", "1.5d", "-1h"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_interval(value)

    def test_next_trigger_preserves_cadence_and_skips_backlog(self) -> None:
        self.assertEqual(next_trigger_after(100.0, 10.0, 100.0), 110.0)
        self.assertEqual(next_trigger_after(100.0, 10.0, 135.0), 140.0)

    def test_initial_scheduler_trigger_runs_immediately(self) -> None:
        stop_event = Event()
        events = []

        class Manager:
            def launch(self, source: str) -> int:
                events.append(source)
                stop_event.set()
                return 123

            def reap_finished(self) -> None:
                return None

        return_code = run_scheduler(
            SchedulerConfig(interval_seconds=86_400),
            stop_event,
            manager=Manager(),
            monotonic=lambda: 100.0,
            cleanup=lambda: events.append("cleanup"),
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(events, ["cleanup", "startup"])

    def test_interval_cleanup_failure_still_starts_the_batch(self) -> None:
        stop_event = Event()
        sources = []

        class Manager:
            def launch(self, source: str) -> int:
                sources.append(source)
                stop_event.set()
                return 123

            def reap_finished(self) -> None:
                return None

        def fail_cleanup() -> None:
            raise RuntimeError("image store is busy")

        return_code = run_scheduler(
            SchedulerConfig(interval_seconds=86_400),
            stop_event,
            manager=Manager(),
            monotonic=lambda: 100.0,
            cleanup=fail_cleanup,
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(sources, ["startup"])

    def test_default_interval_cleanup_runs_apply_before_the_batch(self) -> None:
        stop_event = Event()
        launched = []

        class Manager:
            def launch(self, source: str) -> int:
                launched.append(source)
                stop_event.set()
                return 1

            def reap_finished(self) -> None:
                return None

        class Completed:
            returncode = 0

        with patch("scrapers.scheduler.subprocess.run", return_value=Completed()) as run:
            return_code = run_scheduler(
                SchedulerConfig(interval_seconds=86_400),
                stop_event,
                manager=Manager(),
                monotonic=lambda: 100.0,
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(launched, ["startup"])
        command = run.call_args.args[0]
        self.assertEqual(
            command[-3:],
            ["-m", "scrapers.image_cleanup", "--apply"],
        )
        self.assertEqual(run.call_args.kwargs["check"], False)

    def test_interval_cleanup_continues_after_nonzero_exit_or_start_failure(self) -> None:
        class Completed:
            returncode = 1

        with patch("scrapers.scheduler.subprocess.run", return_value=Completed()):
            run_interval_cleanup()

        with patch(
            "scrapers.scheduler.subprocess.run",
            side_effect=OSError("cleanup missing"),
        ):
            run_interval_cleanup()

    def test_interval_cleanup_skips_when_image_store_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "images"
            log_dir = Path(tmp_dir) / "logs"
            root.mkdir()
            log_dir.mkdir()
            kept = root / "keep.jpg"
            kept.write_bytes(b"keep")
            env = {
                "SMARTMATCH_IMAGES_DIR": str(root),
                "SMARTMATCH_LOG_LEVEL": "ALL",
                "SMARTMATCH_LOG_DIR": str(log_dir),
                "SMARTMATCH_CONTAINER_NAME": "scrapers-test",
            }
            with image_storage_lock(root, exclusive=False), patch.dict(
                "os.environ", env, clear=False
            ):
                run_interval_cleanup()

            self.assertEqual(kept.read_bytes(), b"keep")
            log_text = "\n".join(
                path.read_text() for path in log_dir.glob("scrapers-test_*.txt")
            )
            self.assertIn("image store is being written", log_text)

    def test_batch_manager_does_not_block_later_interval_submissions(self) -> None:
        processes = []

        class Process:
            def __init__(self, pid: int) -> None:
                self.pid = pid
                self.return_code = None
                self.wait_calls = 0

            def poll(self):
                return self.return_code

            def wait(self):
                self.wait_calls += 1
                return self.return_code

        def popen(command, **kwargs):
            process = Process(100 + len(processes))
            process.command = command
            process.kwargs = kwargs
            processes.append(process)
            return process

        manager = BatchProcessManager(popen_factory=popen)
        manager.launch("startup")
        manager.launch("scheduled")

        self.assertEqual(len(processes), 2)
        self.assertEqual(processes[0].wait_calls, 0)
        self.assertEqual(
            processes[1].command[1:4],
            ["-m", "scrapers.worker", "run-all"],
        )
        self.assertEqual(processes[1].command[-1], "scheduled")
        self.assertTrue(processes[1].kwargs["cwd"].endswith("smARTmatch"))

        processes[0].return_code = 0
        manager.reap_finished()
        self.assertEqual(processes[0].wait_calls, 1)


if __name__ == "__main__":
    unittest.main()
