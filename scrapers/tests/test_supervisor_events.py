from __future__ import annotations

import io
import signal
import unittest
from unittest import mock

from scrapers import supervisor_events
from scrapers.supervisor_events import handle_event


class SupervisorEventTests(unittest.TestCase):
    def test_fatal_scheduler_stops_supervisor_parent(self) -> None:
        calls = []

        handled = handle_event(
            "PROCESS_STATE_FATAL",
            "processname:scraper-scheduler groupname:scraper-scheduler from_state:BACKOFF",
            parent_pid=123,
            kill=lambda pid, sig: calls.append((pid, sig)),
        )

        self.assertTrue(handled)
        self.assertEqual(calls, [(123, signal.SIGTERM)])

    def test_main_preserves_supervisor_stdout_protocol(self) -> None:
        stdin = io.StringIO(
            "ver:3.0 server:supervisor serial:1 pool:listener "
            "eventname:PROCESS_STATE_EXITED len:0\n"
        )
        stdout = io.StringIO()
        with mock.patch.object(
            supervisor_events, "configure_logging"
        ) as configure, mock.patch.object(
            supervisor_events.sys, "stdin", stdin
        ), mock.patch.object(
            supervisor_events.sys, "stdout", stdout
        ):
            self.assertEqual(supervisor_events.main(), 0)

        configure.assert_called_once_with(console_mode="stderr")
        self.assertEqual(stdout.getvalue(), "READY\nRESULT\n2\nOKREADY\n")

    def test_nonessential_event_is_ignored(self) -> None:
        calls = []

        handled = handle_event(
            "PROCESS_STATE_EXITED",
            "processname:scraper-scheduler groupname:scraper-scheduler",
            parent_pid=123,
            kill=lambda pid, sig: calls.append((pid, sig)),
        )

        self.assertFalse(handled)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
