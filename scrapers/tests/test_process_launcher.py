from __future__ import annotations

import threading
import unittest

from scrapers.process_launcher import WorkerProcessLauncher


class _Process:
    pid = 4321

    def __init__(self) -> None:
        self.release = threading.Event()
        self.waited = threading.Event()

    def wait(self) -> int:
        self.release.wait(timeout=2)
        self.waited.set()
        return 0


class ProcessLauncherTests(unittest.TestCase):
    def test_launch_all_uses_fixed_worker_command_and_reaps_child(self) -> None:
        commands = []
        process = _Process()

        def popen(command, **kwargs):
            commands.append((command, kwargs))
            return process

        launcher = WorkerProcessLauncher(popen_factory=popen)
        result = launcher.launch_all()
        process.release.set()

        self.assertTrue(process.waited.wait(timeout=2))
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["pid"], 4321)
        command, kwargs = commands[0]
        self.assertEqual(command[1:4], ["-m", "scrapers.worker", "run-all"])
        self.assertNotIn("shell", kwargs)
        self.assertTrue(kwargs["cwd"].endswith("smARTmatch"))

    def test_launch_scraper_passes_validated_name_as_one_argument(self) -> None:
        commands = []
        process = _Process()
        launcher = WorkerProcessLauncher(
            popen_factory=lambda command, **_kwargs: commands.append(command) or process
        )

        launcher.launch_scraper("christies")
        process.release.set()

        self.assertTrue(process.waited.wait(timeout=2))
        self.assertEqual(commands[0][4], "christies")

    def test_outstanding_request_limit_is_atomic(self) -> None:
        process = _Process()
        launcher = WorkerProcessLauncher(
            popen_factory=lambda _command, **_kwargs: process,
            max_outstanding=1,
        )
        launcher.launch_all()

        with self.assertRaisesRegex(OSError, "Too many"):
            launcher.launch_all()

        process.release.set()
        self.assertTrue(process.waited.wait(timeout=2))


if __name__ == "__main__":
    unittest.main()
