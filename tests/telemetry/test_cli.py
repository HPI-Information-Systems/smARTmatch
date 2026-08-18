"""Focused offline telemetry tests."""

from __future__ import annotations

import os
import signal
import unittest
from unittest import mock

from telemetry import cli as telemetry


class TelemetryWorkerTests(unittest.TestCase):
    def test_one_shot_exit_code_reports_failure_classification(self) -> None:
        with mock.patch.object(
            telemetry,
            "try_send_startup_telemetry",
            return_value="transient_failure",
        ):
            self.assertEqual(
                telemetry._run_one_shot("startup"),
                telemetry.WORKER_EXIT_TRANSIENT,
            )
        with mock.patch.object(
            telemetry,
            "try_send_startup_telemetry",
            return_value="terminal_failure",
        ):
            self.assertEqual(
                telemetry._run_one_shot("startup"),
                telemetry.WORKER_EXIT_TERMINAL,
            )
        with mock.patch.object(
            telemetry, "try_send_startup_telemetry", return_value="sent"
        ):
            self.assertEqual(telemetry._run_one_shot("startup"), 0)
        with mock.patch.object(
            telemetry, "try_send_daily_telemetry", return_value="already_attempted"
        ):
            self.assertEqual(
                telemetry._run_one_shot("daily"), telemetry.WORKER_EXIT_NOOP
            )

    def test_deadline_unwinds_without_os_exit(self) -> None:
        self.assertFalse(issubclass(telemetry.TelemetryDeadlineExceeded, Exception))
        handlers = {}

        def install_handler(signum, handler):
            previous = handlers.get(signum, signal.SIG_DFL)
            handlers[signum] = handler
            return previous

        def reach_deadline():
            handlers[signal.SIGALRM](signal.SIGALRM, None)

        with mock.patch.object(
            telemetry.signal, "signal", side_effect=install_handler
        ), mock.patch.object(telemetry.signal, "alarm"), mock.patch.object(
            telemetry,
            "try_send_startup_telemetry",
            side_effect=reach_deadline,
        ), mock.patch.object(
            os, "_exit"
        ) as hard_exit:
            self.assertEqual(
                telemetry._run_one_shot("startup"),
                telemetry.WORKER_EXIT_DEADLINE,
            )
        hard_exit.assert_not_called()
