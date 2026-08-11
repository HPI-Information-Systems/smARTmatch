from __future__ import annotations

import signal
import unittest

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
