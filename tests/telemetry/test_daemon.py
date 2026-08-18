"""Focused offline telemetry tests."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from telemetry import daemon as telemetry
from telemetry import database
from telemetry.constants import WORKER_EXIT_TRANSIENT


class _Health:
    def __init__(self):
        self.state = "starting"
        self.updates = []
        self.heartbeats = []

    def update(self, state, detail, **fields):
        self.state = state
        self.updates.append((state, detail, fields))
        return True

    def heartbeat(self, **fields):
        self.heartbeats.append(fields)
        return True


class TelemetryDaemonTests(unittest.TestCase):
    def test_disabled_daemon_reports_healthy_without_launching_work(self) -> None:
        stop_event = mock.Mock()
        stop_event.is_set.side_effect = [False, True]
        health = _Health()
        launch_worker = mock.Mock()
        with mock.patch.object(
            telemetry, "_telemetry_enabled", return_value=False
        ), mock.patch.object(database, "connect") as connect:
            self.assertEqual(
                telemetry.run_telemetry_daemon(
                    stop_event,
                    health=health,
                    launch_worker=launch_worker,
                ),
                0,
            )
        launch_worker.assert_not_called()
        connect.assert_not_called()
        self.assertTrue(
            any(state == "disabled" for state, _detail, _fields in health.updates)
        )

    def test_daemon_runs_startup_then_daily_workers(self) -> None:
        stop_event = mock.Mock()
        stop_event.is_set.side_effect = [False, False, True]
        startup_worker = mock.Mock()
        startup_worker.poll.return_value = 0
        startup_worker.wait.return_value = 0
        daily_worker = mock.Mock()
        daily_worker.poll.return_value = None
        launch = mock.Mock(side_effect=[startup_worker, daily_worker])
        now = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)

        with mock.patch.object(
            telemetry, "_telemetry_enabled", return_value=True
        ), mock.patch.object(telemetry, "_stop_worker") as stop_worker:
            self.assertEqual(
                telemetry.run_telemetry_daemon(
                    stop_event,
                    now_fn=lambda: now,
                    launch_worker=launch,
                ),
                0,
            )

        self.assertEqual(
            [call.args[0] for call in launch.call_args_list], ["startup", "daily"]
        )
        stop_worker.assert_called_once_with(daily_worker)

    def test_failed_startup_worker_is_retried_until_it_succeeds(self) -> None:
        stop_event = mock.Mock()
        stop_event.is_set.side_effect = [False, False, False, True]
        failed_worker = mock.Mock()
        failed_worker.poll.return_value = 1
        failed_worker.wait.return_value = 1
        retry_worker = mock.Mock()
        retry_worker.poll.return_value = None
        launch = mock.Mock(side_effect=[failed_worker, retry_worker])

        with mock.patch.object(
            telemetry, "_telemetry_enabled", return_value=True
        ), mock.patch.object(telemetry, "_stop_worker") as stop_worker, self.assertLogs(
            telemetry.logger, level="ERROR"
        ) as captured_logs:
            self.assertEqual(
                telemetry.run_telemetry_daemon(
                    stop_event,
                    now_fn=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
                    launch_worker=launch,
                ),
                0,
            )

        self.assertIn(
            "Telemetry worker trigger=startup failed exit_code=1",
            "\n".join(captured_logs.output),
        )
        self.assertEqual(
            [call.args[0] for call in launch.call_args_list],
            ["startup", "startup"],
        )
        stop_event.wait.assert_any_call(telemetry.WORKER_LAUNCH_RETRY_SECONDS)
        stop_worker.assert_called_once_with(retry_worker)

    def test_terminal_startup_failure_is_not_retried(self) -> None:
        stop_event = mock.Mock()
        stop_event.is_set.side_effect = [False, False, True]
        terminal_worker = mock.Mock()
        terminal_worker.poll.return_value = telemetry.WORKER_EXIT_TERMINAL
        terminal_worker.wait.return_value = telemetry.WORKER_EXIT_TERMINAL
        daily_worker = mock.Mock()
        daily_worker.poll.return_value = None
        launch = mock.Mock(side_effect=[terminal_worker, daily_worker])
        health = _Health()

        with mock.patch.object(
            telemetry, "_telemetry_enabled", return_value=True
        ), mock.patch.object(telemetry, "_stop_worker") as stop_worker, self.assertLogs(
            telemetry.logger, level="ERROR"
        ) as captured_logs:
            self.assertEqual(
                telemetry.run_telemetry_daemon(
                    stop_event,
                    now_fn=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
                    launch_worker=launch,
                    health=health,
                ),
                0,
            )

        self.assertEqual(
            [call.args[0] for call in launch.call_args_list],
            ["startup", "daily"],
        )
        stop_worker.assert_called_once_with(daily_worker)
        self.assertIn(
            "failed terminally; no startup retry",
            "\n".join(captured_logs.output),
        )
        self.assertTrue(
            any(
                state == "unhealthy" and "terminally" in detail
                for state, detail, _fields in health.updates
            )
        )

    def test_daily_noop_does_not_recover_terminal_startup_health(self) -> None:
        stop_event = mock.Mock()
        stop_event.is_set.side_effect = [False, False, False, True]
        terminal_worker = mock.Mock()
        terminal_worker.poll.return_value = telemetry.WORKER_EXIT_TERMINAL
        terminal_worker.wait.return_value = telemetry.WORKER_EXIT_TERMINAL
        noop_worker = mock.Mock()
        noop_worker.poll.return_value = telemetry.WORKER_EXIT_NOOP
        noop_worker.wait.return_value = telemetry.WORKER_EXIT_NOOP
        health = _Health()
        with mock.patch.object(
            telemetry, "_telemetry_enabled", return_value=True
        ), mock.patch.object(telemetry, "_stop_worker"), self.assertLogs(
            telemetry.logger, level="ERROR"
        ):
            self.assertEqual(
                telemetry.run_telemetry_daemon(
                    stop_event,
                    now_fn=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
                    launch_worker=mock.Mock(side_effect=[terminal_worker, noop_worker]),
                    health=health,
                ),
                0,
            )
        unhealthy_index = next(
            index
            for index, (state, _detail, _fields) in enumerate(health.updates)
            if state == "unhealthy"
        )
        self.assertFalse(
            any(
                state == "healthy"
                for state, _detail, _fields in health.updates[unhealthy_index + 1 :]
            )
        )

    def test_transient_startup_failures_exhaust_retry_budget(self) -> None:
        stop_event = mock.Mock()
        stop_event.is_set.side_effect = [False, False, False, False, True]
        first_worker = mock.Mock()
        first_worker.poll.return_value = WORKER_EXIT_TRANSIENT
        first_worker.wait.return_value = WORKER_EXIT_TRANSIENT
        second_worker = mock.Mock()
        second_worker.poll.return_value = WORKER_EXIT_TRANSIENT
        second_worker.wait.return_value = WORKER_EXIT_TRANSIENT
        daily_worker = mock.Mock()
        daily_worker.poll.return_value = None
        launch = mock.Mock(side_effect=[first_worker, second_worker, daily_worker])
        health = _Health()

        with mock.patch.object(
            telemetry, "_telemetry_enabled", return_value=True
        ), mock.patch.object(
            telemetry, "STARTUP_MAX_TRANSIENT_FAILURES", 2
        ), mock.patch.object(
            telemetry, "_stop_worker"
        ) as stop_worker, self.assertLogs(
            telemetry.logger, level="ERROR"
        ) as captured_logs:
            self.assertEqual(
                telemetry.run_telemetry_daemon(
                    stop_event,
                    now_fn=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
                    launch_worker=launch,
                    health=health,
                ),
                0,
            )

        self.assertEqual(
            [call.args[0] for call in launch.call_args_list],
            ["startup", "startup", "daily"],
        )
        stop_worker.assert_called_once_with(daily_worker)
        self.assertTrue(
            any(
                29.0 <= call.args[0] <= telemetry.WORKER_LAUNCH_RETRY_SECONDS
                for call in stop_event.wait.call_args_list
            )
        )
        self.assertIn(
            "retry budget exhausted failures=2",
            "\n".join(captured_logs.output),
        )
        self.assertTrue(
            any(
                state == "unhealthy" and "retry budget exhausted" in detail
                for state, detail, _fields in health.updates
            )
        )

    def test_long_retry_wait_refreshes_health_heartbeat(self) -> None:
        stop_event = mock.Mock()
        stop_event.wait.side_effect = [False, False, True]
        health = _Health()
        with mock.patch.object(
            telemetry.time, "monotonic", side_effect=[0.0, 0.0, 30.0, 60.0]
        ):
            telemetry._wait_with_health(stop_event, 61.0, health, trigger="startup")
        self.assertEqual(stop_event.wait.call_args_list[0], mock.call(30.0))
        self.assertEqual(len(health.heartbeats), 2)

    def test_worker_launch_failure_uses_backoff(self) -> None:
        stop_event = mock.Mock()
        stop_event.is_set.side_effect = [False, True]
        launch = mock.Mock(return_value=None)
        with mock.patch.object(telemetry, "_telemetry_enabled", return_value=True):
            self.assertEqual(
                telemetry.run_telemetry_daemon(
                    stop_event,
                    now_fn=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
                    launch_worker=launch,
                ),
                0,
            )
        stop_event.wait.assert_called_once_with(telemetry.WORKER_LAUNCH_RETRY_SECONDS)

    def test_parent_deadline_forces_cleanup_when_worker_does_not_exit(self) -> None:
        stop_event = mock.Mock()
        stop_event.is_set.side_effect = [False, False, True]
        worker = mock.Mock()
        worker.poll.return_value = None
        launch = mock.Mock(return_value=worker)
        elapsed = (
            telemetry.DEFAULT_PROCESS_DEADLINE_SECONDS
            + telemetry.WORKER_DEADLINE_CLEANUP_GRACE_SECONDS
            + 1
        )

        with mock.patch.object(
            telemetry, "_telemetry_enabled", return_value=True
        ), mock.patch.object(
            telemetry.time, "monotonic", side_effect=[0.0, elapsed]
        ), mock.patch.object(
            telemetry, "_stop_worker"
        ) as stop_worker:
            self.assertEqual(
                telemetry.run_telemetry_daemon(
                    stop_event,
                    now_fn=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
                    launch_worker=launch,
                ),
                0,
            )

        stop_worker.assert_called_once_with(worker)
        stop_event.wait.assert_any_call(telemetry.WORKER_LAUNCH_RETRY_SECONDS)

    def test_worker_is_a_separate_process(self) -> None:
        process = mock.Mock()
        with mock.patch.object(
            telemetry.subprocess, "Popen", return_value=process
        ) as popen:
            self.assertIs(telemetry._launch_worker("startup"), process)
        self.assertEqual(
            popen.call_args.args[0],
            (
                telemetry.sys.executable,
                "-m",
                telemetry.TELEMETRY_MODULE,
                "--trigger",
                "startup",
            ),
        )
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
