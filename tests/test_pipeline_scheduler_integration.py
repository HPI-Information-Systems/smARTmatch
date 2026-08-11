"""Lightweight subprocess integration tests for the pipeline scheduler."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest import mock

from scripts import run_pipeline_scheduler as scheduler


class PipelineSchedulerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        scheduler._set_child_subreaper(True)

    @classmethod
    def tearDownClass(cls) -> None:
        scheduler._set_child_subreaper(False)

    def test_cycle_runs_fresh_processes_sequentially(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events = root / "events.txt"
            first = self._recording_step("first", events, 0.1)
            second = self._recording_step("second", events, 0.0)
            with mock.patch.object(scheduler, "PIPELINE_STEPS", (first, second)):
                self.assertEqual(scheduler._run_cycle(1, Event()), [])

            rows = events.read_text().splitlines()
            self.assertEqual([row.split(":")[0] for row in rows], ["first", "first", "second", "second"])
            self.assertEqual([row.split(":")[1] for row in rows], ["start", "end", "start", "end"])
            self.assertNotEqual(rows[0].split(":")[2], rows[2].split(":")[2])

    def test_run_step_terminates_orphaned_process_group_before_returning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            group_file = root / "group.txt"
            code = (
                "import os, pathlib, subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
                "start_new_session=True); "
                f"pathlib.Path({str(group_file)!r}).write_text(str(os.getpgrp()))"
            )
            step = scheduler.PipelineStep(
                "orphan-test", "orphan test", (sys.executable, "-c", code), root
            )
            self.assertEqual(scheduler._run_step(step, Event()), 0)
            process_group_id = int(group_file.read_text())
            self.assertFalse(scheduler._process_group_exists(process_group_id))

    @staticmethod
    def _recording_step(
        name: str, events: Path, delay_seconds: float
    ) -> scheduler.PipelineStep:
        code = (
            "import os, pathlib, time; "
            f"p=pathlib.Path({str(events)!r}); "
            f"p.open('a').write('{name}:start:' + str(os.getpid()) + '\\n'); "
            f"time.sleep({delay_seconds}); "
            f"p.open('a').write('{name}:end:' + str(os.getpid()) + '\\n')"
        )
        return scheduler.PipelineStep(name, name, (sys.executable, "-c", code), events.parent)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
