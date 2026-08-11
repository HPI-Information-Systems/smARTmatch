"""Process lifecycle tests for the combined pipeline scheduler."""

from __future__ import annotations

import os
import signal
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from scripts import run_pipeline_scheduler as scheduler


class StepProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.step = scheduler.PipelineStep(
            "test", "test step", ("python", "worker.py"), Path("/tmp")
        )

    def test_run_step_waits_for_process_and_cleans_group(self) -> None:
        process = mock.Mock(pid=123)
        process.poll.side_effect = [None, 0]
        process.wait.return_value = 0
        stop_event = mock.Mock()
        stop_event.wait.return_value = False
        with mock.patch.object(
            scheduler.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(
            scheduler, "_stop_lingering_process_group"
        ) as cleanup:
            self.assertEqual(
                scheduler._run_step(
                    self.step, stop_event, extra_env={"PIPELINE_TEST": "1"}
                ),
                0,
            )

        kwargs = popen.call_args.kwargs
        self.assertEqual(popen.call_args.args[0], self.step.command)
        self.assertEqual(kwargs["cwd"], self.step.cwd)
        self.assertEqual(kwargs["env"]["PIPELINE_TEST"], "1")
        self.assertTrue(kwargs["start_new_session"])
        cleanup.assert_called_once_with(123)

    def test_run_step_stops_process_when_shutdown_is_requested(self) -> None:
        process = mock.Mock(pid=456)
        process.poll.return_value = None
        process.wait.return_value = -signal.SIGTERM
        stop_event = mock.Mock()
        stop_event.wait.return_value = True
        with mock.patch.object(
            scheduler.subprocess, "Popen", return_value=process
        ), mock.patch.object(scheduler, "_stop_process") as stop_process, mock.patch.object(
            scheduler, "_stop_lingering_process_group"
        ) as cleanup:
            self.assertEqual(scheduler._run_step(self.step, stop_event), -signal.SIGTERM)

        stop_process.assert_called_once_with(process)
        cleanup.assert_called_once_with(456)

    def test_run_step_returns_127_when_process_cannot_start(self) -> None:
        with mock.patch.object(
            scheduler.subprocess, "Popen", side_effect=OSError("missing")
        ):
            self.assertEqual(scheduler._run_step(self.step, mock.Mock()), 127)


class DirectProcessShutdownTests(unittest.TestCase):
    def test_stop_process_returns_when_process_already_finished(self) -> None:
        process = mock.Mock()
        process.poll.return_value = 0
        with mock.patch.object(scheduler, "_signal_process_group") as send_signal:
            scheduler._stop_process(process)
        send_signal.assert_not_called()

    def test_stop_process_sends_sigterm_and_waits(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(scheduler, "_signal_process_group") as send_signal:
            scheduler._stop_process(process)
        send_signal.assert_called_once_with(process, signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=scheduler.SHUTDOWN_TIMEOUT_SECONDS)

    def test_stop_process_kills_after_timeout(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("worker", scheduler.SHUTDOWN_TIMEOUT_SECONDS),
            0,
        ]
        with mock.patch.object(scheduler, "_signal_process_group") as send_signal:
            scheduler._stop_process(process)
        self.assertEqual(
            send_signal.call_args_list,
            [mock.call(process, signal.SIGTERM), mock.call(process, signal.SIGKILL)],
        )
        self.assertEqual(process.wait.call_count, 2)


class ProcessGroupCleanupTests(unittest.TestCase):
    def test_cleanup_returns_when_group_is_already_gone(self) -> None:
        with mock.patch.object(
            scheduler, "_process_group_exists", return_value=False
        ), mock.patch.object(scheduler, "_signal_process_group_id") as send_signal:
            scheduler._stop_lingering_process_group(100)
        send_signal.assert_not_called()

    def test_cleanup_terminates_lingering_group(self) -> None:
        with mock.patch.object(
            scheduler, "_process_group_exists", return_value=True
        ), mock.patch.object(
            scheduler, "_wait_for_process_group_exit", return_value=True
        ), mock.patch.object(scheduler, "_signal_process_group_id") as send_signal:
            scheduler._stop_lingering_process_group(101)
        send_signal.assert_called_once_with(101, signal.SIGTERM)

    def test_cleanup_kills_group_that_ignores_sigterm(self) -> None:
        with mock.patch.object(
            scheduler, "_process_group_exists", return_value=True
        ), mock.patch.object(
            scheduler, "_wait_for_process_group_exit", side_effect=[False, True]
        ), mock.patch.object(scheduler, "_signal_process_group_id") as send_signal:
            scheduler._stop_lingering_process_group(102)
        self.assertEqual(
            send_signal.call_args_list,
            [mock.call(102, signal.SIGTERM), mock.call(102, signal.SIGKILL)],
        )

    def test_cleanup_fails_closed_when_group_survives_sigkill(self) -> None:
        with mock.patch.object(
            scheduler, "_process_group_exists", return_value=True
        ), mock.patch.object(
            scheduler, "_wait_for_process_group_exit", return_value=False
        ), mock.patch.object(scheduler, "_signal_process_group_id"), self.assertRaisesRegex(
            RuntimeError, "survived SIGKILL"
        ):
            scheduler._stop_lingering_process_group(103)

    def test_wait_for_group_returns_after_group_exits(self) -> None:
        with mock.patch.object(
            scheduler, "_process_group_exists", side_effect=[True, False]
        ), mock.patch.object(scheduler, "_reap_process_group_children") as reap, mock.patch.object(
            scheduler.time, "monotonic", side_effect=[10.0, 10.1]
        ), mock.patch.object(scheduler.time, "sleep") as sleep:
            self.assertTrue(scheduler._wait_for_process_group_exit(104, 1.0))
        reap.assert_called_once_with(104)
        sleep.assert_called_once_with(scheduler.POLL_SECONDS)

    def test_wait_for_group_times_out(self) -> None:
        with mock.patch.object(
            scheduler, "_process_group_exists", return_value=True
        ), mock.patch.object(scheduler, "_reap_process_group_children"), mock.patch.object(
            scheduler.time, "monotonic", side_effect=[10.0, 11.0]
        ):
            self.assertFalse(scheduler._wait_for_process_group_exit(105, 0.5))

    def test_reap_group_children_until_none_are_ready(self) -> None:
        with mock.patch.object(
            scheduler.os, "waitpid", side_effect=[(200, 0), (0, 0)]
        ) as waitpid:
            scheduler._reap_process_group_children(106)
        self.assertEqual(waitpid.call_count, 2)
        waitpid.assert_called_with(-106, os.WNOHANG)

    def test_reap_group_handles_no_child_processes(self) -> None:
        with mock.patch.object(
            scheduler.os, "waitpid", side_effect=ChildProcessError
        ):
            scheduler._reap_process_group_children(107)

    def test_process_group_exists_uses_proc_when_available(self) -> None:
        proc_root = mock.Mock()
        proc_root.is_dir.return_value = True
        with mock.patch.object(scheduler, "_PROC_ROOT", proc_root), mock.patch.object(
            scheduler, "_process_group_exists_in_proc", return_value=True
        ) as proc_check:
            self.assertTrue(scheduler._process_group_exists(108))
        proc_check.assert_called_once_with(108)

    def test_process_group_exists_fallback_outcomes(self) -> None:
        proc_root = mock.Mock()
        proc_root.is_dir.return_value = False
        with mock.patch.object(scheduler, "_PROC_ROOT", proc_root), mock.patch.object(
            scheduler.os, "killpg"
        ):
            self.assertTrue(scheduler._process_group_exists(108))
        with mock.patch.object(scheduler, "_PROC_ROOT", proc_root), mock.patch.object(
            scheduler.os, "killpg", side_effect=ProcessLookupError
        ):
            self.assertFalse(scheduler._process_group_exists(108))
        with mock.patch.object(scheduler, "_PROC_ROOT", proc_root), mock.patch.object(
            scheduler.os, "killpg", side_effect=PermissionError
        ):
            self.assertTrue(scheduler._process_group_exists(108))

    def test_proc_group_check_ignores_zombies_malformed_and_raced_files(self) -> None:
        raced = mock.Mock()
        raced.read_text.side_effect = OSError
        malformed = mock.Mock()
        malformed.read_text.return_value = "bad"
        zombie = mock.Mock()
        zombie.read_text.return_value = "12 (worker) Z 1 109 0 0"
        live = mock.Mock()
        live.read_text.return_value = "13 (worker) S 1 109 0 0"
        proc_root = mock.Mock()
        with mock.patch.object(scheduler, "_PROC_ROOT", proc_root):
            proc_root.glob.return_value = [raced, malformed, zombie, live]
            self.assertTrue(scheduler._process_group_exists_in_proc(109))
            proc_root.glob.return_value = [raced, malformed, zombie]
            self.assertFalse(scheduler._process_group_exists_in_proc(109))

    def test_parse_proc_stat_validates_process_fields(self) -> None:
        self.assertEqual(
            scheduler._parse_proc_stat("12 (worker) S 1 109 0"),
            scheduler.ProcStat(12, "S", 1, 109),
        )
        self.assertEqual(
            scheduler._parse_proc_stat("12 (worker) Z 1 109 0"),
            scheduler.ProcStat(12, "Z", 1, 109),
        )
        self.assertIsNone(scheduler._parse_proc_stat("bad"))
        self.assertIsNone(scheduler._parse_proc_stat("12 (worker) S 1 invalid 0"))
        self.assertIsNone(scheduler._parse_proc_stat("invalid (worker) S 1 109 0"))

    def test_signal_helpers_send_to_group_and_ignore_missing_group(self) -> None:
        process = mock.Mock(pid=109)
        with mock.patch.object(scheduler.os, "killpg") as killpg:
            scheduler._signal_process_group(process, signal.SIGTERM)
        killpg.assert_called_once_with(109, signal.SIGTERM)

        with mock.patch.object(
            scheduler.os, "killpg", side_effect=ProcessLookupError
        ):
            scheduler._signal_process_group_id(109, signal.SIGKILL)


class DetachedDescendantTests(unittest.TestCase):
    def test_stop_new_children_returns_when_none_exist(self) -> None:
        with mock.patch.object(
            scheduler, "_new_child_process_ids", return_value=set()
        ), mock.patch.object(scheduler, "_signal_and_wait_for_new_children") as wait:
            scheduler._stop_new_child_processes(set())
        wait.assert_not_called()

    def test_stop_new_children_terminates_then_kills_when_needed(self) -> None:
        with mock.patch.object(
            scheduler, "_new_child_process_ids", return_value={10}
        ), mock.patch.object(
            scheduler, "_signal_and_wait_for_new_children", return_value=True
        ) as wait:
            scheduler._stop_new_child_processes(set())
        wait.assert_called_once_with(set(), signal.SIGTERM, scheduler.SHUTDOWN_TIMEOUT_SECONDS)

        with mock.patch.object(
            scheduler, "_new_child_process_ids", return_value={10}
        ), mock.patch.object(
            scheduler, "_signal_and_wait_for_new_children", side_effect=[False, True]
        ) as wait:
            scheduler._stop_new_child_processes(set())
        self.assertEqual(
            wait.call_args_list,
            [
                mock.call(set(), signal.SIGTERM, scheduler.SHUTDOWN_TIMEOUT_SECONDS),
                mock.call(set(), signal.SIGKILL, scheduler.SHUTDOWN_TIMEOUT_SECONDS),
            ],
        )

    def test_stop_new_children_fails_closed_after_sigkill(self) -> None:
        with mock.patch.object(
            scheduler, "_new_child_process_ids", return_value={10}
        ), mock.patch.object(
            scheduler, "_signal_and_wait_for_new_children", return_value=False
        ), self.assertRaisesRegex(RuntimeError, "descendants survived SIGKILL"):
            scheduler._stop_new_child_processes(set())

    def test_signal_and_wait_for_children_exits_after_reaping(self) -> None:
        with mock.patch.object(
            scheduler, "_new_child_process_ids", side_effect=[{11}, set()]
        ), mock.patch.object(scheduler, "_signal_process") as send_signal, mock.patch.object(
            scheduler, "_reap_process"
        ) as reap, mock.patch.object(
            scheduler.time, "monotonic", side_effect=[1.0, 1.1]
        ), mock.patch.object(scheduler.time, "sleep") as sleep:
            self.assertTrue(
                scheduler._signal_and_wait_for_new_children(set(), signal.SIGTERM, 1.0)
            )
        send_signal.assert_called_once_with(11, signal.SIGTERM)
        reap.assert_called_once_with(11)
        sleep.assert_called_once_with(scheduler.POLL_SECONDS)

    def test_signal_and_wait_for_children_times_out(self) -> None:
        with mock.patch.object(
            scheduler, "_new_child_process_ids", return_value={12}
        ), mock.patch.object(scheduler, "_signal_process"), mock.patch.object(
            scheduler, "_reap_process"
        ), mock.patch.object(scheduler.time, "monotonic", side_effect=[1.0, 2.0]):
            self.assertFalse(
                scheduler._signal_and_wait_for_new_children(set(), signal.SIGTERM, 0.5)
            )

    def test_child_process_queries_exclude_baseline(self) -> None:
        stats = [
            scheduler.ProcStat(20, "S", 5, 20),
            scheduler.ProcStat(21, "S", 6, 21),
        ]
        with mock.patch.object(scheduler, "_iter_proc_stats", return_value=iter(stats)):
            self.assertEqual(scheduler._child_process_ids(5), {20})
        with mock.patch.object(
            scheduler, "_child_process_ids", return_value={20, 22}
        ), mock.patch.object(scheduler.os, "getpid", return_value=5):
            self.assertEqual(scheduler._new_child_process_ids({20}), {22})

    def test_subreaper_setup_success_and_failures(self) -> None:
        libc = mock.Mock()
        libc.prctl.return_value = 0
        with mock.patch.object(scheduler.ctypes, "CDLL", return_value=libc):
            scheduler._set_child_subreaper(True)
        libc.prctl.assert_called_once_with(scheduler._PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)

        libc.prctl.return_value = 1
        with mock.patch.object(scheduler.ctypes, "CDLL", return_value=libc), mock.patch.object(
            scheduler.ctypes, "get_errno", return_value=5
        ), self.assertRaises(OSError):
            scheduler._set_child_subreaper(False)

        with mock.patch.object(scheduler.sys, "platform", "darwin"), self.assertRaisesRegex(
            RuntimeError, "requires Linux"
        ):
            scheduler._set_child_subreaper(True)

    def test_signal_and_reap_process_helpers_handle_missing_children(self) -> None:
        with mock.patch.object(scheduler.os, "kill") as kill_process:
            scheduler._signal_process(30, signal.SIGTERM)
        kill_process.assert_called_once_with(30, signal.SIGTERM)
        with mock.patch.object(scheduler.os, "kill", side_effect=ProcessLookupError):
            scheduler._signal_process(30, signal.SIGKILL)

        with mock.patch.object(scheduler.os, "waitpid") as waitpid:
            scheduler._reap_process(30)
        waitpid.assert_called_once_with(30, os.WNOHANG)
        with mock.patch.object(scheduler.os, "waitpid", side_effect=ChildProcessError):
            scheduler._reap_process(30)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
