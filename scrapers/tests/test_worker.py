from __future__ import annotations

import unittest
from types import SimpleNamespace

from unittest import mock

from scrapers import worker
from scrapers.scope import DASHBOARD_SCRAPER_NAMES
from scrapers.worker import run_batch, run_one


class WorkerTests(unittest.TestCase):
    def test_dashboard_scope_is_explicit_and_excludes_lostart(self) -> None:
        self.assertEqual(
            DASHBOARD_SCRAPER_NAMES,
            ("christies", "sothebys", "drouot", "lottissimo", "dorotheum"),
        )
        self.assertNotIn("lostart", DASHBOARD_SCRAPER_NAMES)

    def test_batch_starts_every_child_before_waiting(self) -> None:
        started = []
        waited = []

        class Process:
            def __init__(self, command) -> None:
                self.command = command

            def wait(self) -> int:
                self_test.assertEqual(len(started), len(DASHBOARD_SCRAPER_NAMES))
                waited.append(self.command)
                return 0

        def popen(command, **_kwargs):
            process = Process(command)
            started.append(process)
            return process

        self_test = self
        return_code = run_batch(source="scheduled", popen_factory=popen)

        self.assertEqual(return_code, 0)
        self.assertEqual(len(waited), len(DASHBOARD_SCRAPER_NAMES))
        self.assertEqual(
            [process.command[4] for process in started],
            list(DASHBOARD_SCRAPER_NAMES),
        )

    def test_batch_waits_for_siblings_after_one_failure(self) -> None:
        waited = []

        class Process:
            def __init__(self, index: int) -> None:
                self.index = index

            def wait(self) -> int:
                waited.append(self.index)
                return 1 if self.index == 0 else 0

        processes = []

        def popen(_command, **_kwargs):
            process = Process(len(processes))
            processes.append(process)
            return process

        return_code = run_batch(source="manual", popen_factory=popen)

        self.assertEqual(return_code, 1)
        self.assertCountEqual(waited, range(len(DASHBOARD_SCRAPER_NAMES)))

    def test_run_one_treats_lock_skip_as_success(self) -> None:
        orchestrator = SimpleNamespace(
            run_scraper=lambda _name: {"status": "skipped", "reason": "already_running"}
        )

        return_code = run_one(
            "christies",
            source="manual",
            orchestrator_factory=lambda: orchestrator,
        )

        self.assertEqual(return_code, 0)

    def test_main_configures_shared_logging(self) -> None:
        with mock.patch.object(worker, "configure_logging") as configure, mock.patch.object(
            worker, "_parse_args", return_value=SimpleNamespace(command="run", scraper="christies", source="manual")
        ), mock.patch.object(worker, "run_one", return_value=0):
            self.assertEqual(worker.main(), 0)

        configure.assert_called_once_with()

    def test_run_one_propagates_scraper_failure(self) -> None:
        orchestrator = SimpleNamespace(
            run_scraper=lambda _name: {"status": "failed", "error": "boom"}
        )

        return_code = run_one(
            "christies",
            source="scheduled",
            orchestrator_factory=lambda: orchestrator,
        )

        self.assertEqual(return_code, 1)


if __name__ == "__main__":
    unittest.main()
