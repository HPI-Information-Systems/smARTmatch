"""Work-aware daemon health status tests."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from shared.service_health import HealthReporter, MAX_STATUS_BYTES, read_health_status


class ServiceHealthTests(unittest.TestCase):
    def test_reporter_writes_atomic_healthy_and_unhealthy_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "health.json"
            reporter = HealthReporter(
                "matching_pipeline", path, heartbeat_interval_seconds=0
            )

            self.assertTrue(
                reporter.update(
                    "running",
                    "pipeline step running",
                    cycle=3,
                    stage="image-matching",
                )
            )
            healthy, message, document = read_health_status(path)
            self.assertTrue(healthy)
            self.assertIn("running", message)
            self.assertEqual(document["cycle"], 3)
            self.assertEqual(document["stage"], "image-matching")

            self.assertTrue(
                reporter.update(
                    "unhealthy",
                    "pipeline cycle failed",
                    failed_steps=["image matching"],
                )
            )
            healthy, message, document = read_health_status(path)
            self.assertFalse(healthy)
            self.assertIn("pipeline cycle failed", message)
            self.assertEqual(document["failed_steps"], ["image matching"])
            self.assertEqual(list(path.parent.glob(".health.json.*")), [])

    def test_disabled_is_healthy_but_stale_status_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "health.json"
            reporter = HealthReporter("telemetry", path)
            reporter.update("disabled", "telemetry is intentionally disabled")
            document = json.loads(path.read_text())

            healthy, _message, _document = read_health_status(
                path,
                max_age_seconds=180,
                now_epoch=document["updated_at_epoch"] + 179,
            )
            self.assertTrue(healthy)
            healthy, message, _document = read_health_status(
                path,
                max_age_seconds=180,
                now_epoch=document["updated_at_epoch"] + 181,
            )
            self.assertFalse(healthy)
            self.assertIn("stale", message)

    def test_heartbeat_refreshes_active_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "health.json"
            reporter = HealthReporter(
                "matching_pipeline", path, heartbeat_interval_seconds=0
            )
            reporter.update("running", "long pipeline stage", stage="blocking")
            first_timestamp = json.loads(path.read_text())["updated_at_epoch"]
            time.sleep(0.01)
            reporter.heartbeat(stage="blocking")
            second_timestamp = json.loads(path.read_text())["updated_at_epoch"]
            self.assertGreaterEqual(second_timestamp, first_timestamp)

    def test_oversized_status_is_rejected_without_an_unbounded_read(self) -> None:
        path = Path("health.json")
        handle = mock.MagicMock()
        handle.__enter__.return_value = handle
        handle.read.return_value = b"x" * (MAX_STATUS_BYTES + 1)

        with mock.patch.object(Path, "open", return_value=handle):
            healthy, message, document = read_health_status(path)

        self.assertFalse(healthy)
        self.assertIn("size limit", message)
        self.assertIsNone(document)
        handle.read.assert_called_once_with(MAX_STATUS_BYTES + 1)

    def test_missing_malformed_and_future_statuses_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "health.json"
            healthy, message, _document = read_health_status(path)
            self.assertFalse(healthy)
            self.assertIn("unavailable", message)

            path.write_text("not-json")
            healthy, message, _document = read_health_status(path)
            self.assertFalse(healthy)
            self.assertIn("not valid", message)

            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "service": "telemetry",
                        "state": "healthy",
                        "detail": "successful work",
                        "updated_at": "1970-01-01T00:08:20Z",
                        "updated_at_epoch": 500,
                    }
                )
            )
            healthy, message, _document = read_health_status(path, now_epoch=400)
            self.assertFalse(healthy)
            self.assertIn("future", message)

            path.write_text(
                json.dumps(
                    {
                        "schema_version": True,
                        "service": "telemetry",
                        "state": "healthy",
                        "detail": "bad schema",
                        "updated_at": "1970-01-01T00:08:20Z",
                        "updated_at_epoch": True,
                    }
                )
            )
            healthy, message, _document = read_health_status(path, now_epoch=1)
            self.assertFalse(healthy)
            self.assertIn("unsupported schema", message)

            malformed_state = json.loads(path.read_text())
            malformed_state["schema_version"] = 1
            malformed_state["updated_at_epoch"] = 1
            malformed_state["state"] = []
            path.write_text(json.dumps(malformed_state))
            healthy, message, _document = read_health_status(path, now_epoch=1)
            self.assertFalse(healthy)
            self.assertIn("unknown state", message)


if __name__ == "__main__":
    unittest.main()
