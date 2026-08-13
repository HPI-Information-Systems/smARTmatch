"""Orchestration and cadence tests for the combined pipeline scheduler."""

from __future__ import annotations

import argparse
import signal
import sys
import unittest
from threading import Event
from unittest import mock

from scripts import run_pipeline_scheduler as scheduler


class PipelineCycleTests(unittest.TestCase):
    def test_pipeline_steps_are_explicit_and_ordered(self) -> None:
        self.assertEqual(
            [step.key for step in scheduler.PIPELINE_STEPS],
            [
                "image-blocking",
                "image-matching",
                "metadata-extraction",
                "metadata-matching",
            ],
        )
        self.assertEqual(
            [step.command[2] for step in scheduler.PIPELINE_STEPS],
            [
                "matching_pipeline.image_blocking",
                "matching_pipeline.image_matching",
                "matching_pipeline.metadata_extraction",
                "matching_pipeline.metadata_matching",
            ],
        )
        self.assertTrue(
            all(step.cwd == scheduler._APP_ROOT for step in scheduler.PIPELINE_STEPS)
        )

    def test_cycle_continues_after_failure(self) -> None:
        calls = []

        def run_step(step, _stop_event, *, extra_env=None):
            calls.append((step.key, dict(extra_env or {})))
            return 7 if step.key == "image-blocking" else 0

        with mock.patch.object(scheduler, "_run_step", side_effect=run_step):
            failures = scheduler._run_cycle(1, Event())

        self.assertEqual([key for key, _env in calls], [step.key for step in scheduler.PIPELINE_STEPS])
        self.assertEqual(failures, ["image blocking"])
        self.assertEqual(calls[1][1], {scheduler.SKIP_IMAGE_MATCHING_ENV: "1"})

    def test_successful_cycle_enables_image_matching(self) -> None:
        calls = []

        def run_step(step, _stop_event, *, extra_env=None):
            calls.append((step.key, dict(extra_env or {})))
            return 0

        with mock.patch.object(scheduler, "_run_step", side_effect=run_step):
            self.assertEqual(scheduler._run_cycle(1, Event()), [])

        self.assertEqual(
            calls[1],
            ("image-matching", {scheduler.SKIP_IMAGE_MATCHING_ENV: "0"}),
        )

    def test_stopped_cycle_does_not_start_steps(self) -> None:
        stop_event = Event()
        stop_event.set()
        with mock.patch.object(scheduler, "_run_step") as run_step:
            self.assertEqual(scheduler._run_cycle(1, stop_event), [])
        run_step.assert_not_called()


class SchedulerCadenceTests(unittest.TestCase):
    def test_scheduler_runs_immediately_and_stops_cleanly(self) -> None:
        stop_event = Event()
        cycles = []

        def run_cycle(number, event):
            cycles.append(number)
            event.set()
            return []

        with mock.patch.object(scheduler, "_run_cycle", side_effect=run_cycle):
            self.assertEqual(scheduler.run_scheduler(60.0, stop_event), 0)
        self.assertEqual(cycles, [1])

    def test_scheduler_waits_until_next_trigger(self) -> None:
        stop_event = mock.Mock()
        stop_event.is_set.side_effect = [False, True]
        with mock.patch.object(
            scheduler.time, "monotonic", side_effect=[100.0, 90.0]
        ), mock.patch.object(scheduler, "_run_cycle") as run_cycle:
            self.assertEqual(scheduler.run_scheduler(60.0, stop_event), 0)
        stop_event.wait.assert_called_once_with(scheduler.POLL_SECONDS)
        run_cycle.assert_not_called()

    def test_next_trigger_handles_current_and_elapsed_intervals(self) -> None:
        self.assertEqual(scheduler._next_trigger_after(10.0, 60.0, 20.0), 70.0)
        self.assertEqual(scheduler._next_trigger_after(10.0, 60.0, 135.0), 190.0)


class SchedulerCliTests(unittest.TestCase):
    def test_parse_args_defaults_to_one_minute(self) -> None:
        with mock.patch.object(sys, "argv", ["scheduler"]):
            self.assertEqual(scheduler._parse_args().interval_minutes, 1.0)

    def test_parse_args_accepts_custom_positive_interval(self) -> None:
        with mock.patch.object(
            sys, "argv", ["scheduler", "--interval-minutes", "2.5"]
        ):
            self.assertEqual(scheduler._parse_args().interval_minutes, 2.5)

    def test_parse_args_rejects_nonpositive_interval(self) -> None:
        with mock.patch.object(
            sys, "argv", ["scheduler", "--interval-minutes", "0"]
        ), self.assertRaises(SystemExit):
            scheduler._parse_args()

    def test_main_wires_arguments_signals_and_scheduler(self) -> None:
        args = argparse.Namespace(interval_minutes=2.0)
        with mock.patch.object(scheduler, "_parse_args", return_value=args), mock.patch.object(
            scheduler, "_set_child_subreaper"
        ) as set_subreaper, mock.patch.object(
            scheduler, "_install_signal_handlers"
        ) as install_handlers, mock.patch.object(
            scheduler, "run_scheduler", return_value=9
        ) as run_scheduler:
            self.assertEqual(scheduler.main(), 9)
        set_subreaper.assert_called_once_with(True)
        stop_event = install_handlers.call_args.args[0]
        run_scheduler.assert_called_once_with(120.0, stop_event)

    def test_installed_signal_handler_sets_stop_event(self) -> None:
        stop_event = Event()
        with mock.patch.object(signal, "signal") as install:
            scheduler._install_signal_handlers(stop_event)
        self.assertEqual([call.args[0] for call in install.call_args_list], [signal.SIGTERM, signal.SIGINT])
        install.call_args_list[0].args[1](signal.SIGTERM, None)
        self.assertTrue(stop_event.is_set())

    def test_log_uses_python_logging(self) -> None:
        with self.assertLogs(scheduler.logger, level="INFO") as captured:
            scheduler._log("hello")
        self.assertIn("hello", captured.output[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
