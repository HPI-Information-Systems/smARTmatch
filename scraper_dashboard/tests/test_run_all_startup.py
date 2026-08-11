from __future__ import annotations

import importlib
import sys
import unittest
from unittest.mock import patch


class _FakeDatabase:
    pass


class _FakeOrchestrator:
    def __init__(self) -> None:
        pass


class _FakeLauncher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def launch_all(self) -> dict:
        self.calls.append("run-all")
        return {"request_id": "batch-1", "pid": 123, "status": "submitted"}

    def launch_scraper(self, scraper_name: str) -> dict:
        self.calls.append(scraper_name)
        return {"request_id": "single-1", "pid": 124, "status": "submitted"}


class DashboardRunAllStartupTests(unittest.TestCase):
    def _import_fresh_app_module(self):
        previous = sys.modules.pop("scraper_dashboard.app", None)

        def cleanup() -> None:
            sys.modules.pop("scraper_dashboard.app", None)
            if previous is not None:
                sys.modules["scraper_dashboard.app"] = previous

        self.addCleanup(cleanup)

        fake_registry = {
            "christies": {"display_name": "Christie's"},
            "sothebys": {"display_name": "Sotheby's"},
            "lostart": {"display_name": "Lost Art"},
            "dorotheum": {"display_name": "Dorotheum"},
        }
        with (
            patch("scrapers.db_interface.Database", _FakeDatabase),
            patch("scrapers.orchestrator.Orchestrator", _FakeOrchestrator),
            patch("scrapers.orchestrator.SCRAPER_REGISTRY", fake_registry),
            patch("scrapers.process_launcher.WorkerProcessLauncher", _FakeLauncher),
        ):
            return importlib.import_module("scraper_dashboard.app")

    def test_startup_run_all_uses_dashboard_scraper_set(self) -> None:
        app_module = self._import_fresh_app_module()

        results = app_module.run_dashboard_scrapers_background()

        self.assertEqual(app_module.worker_launcher.calls, ["run-all"])
        self.assertEqual(
            results,
            {"request_id": "batch-1", "pid": 123, "status": "submitted"},
        )

    def test_api_run_all_uses_same_dashboard_scraper_set(self) -> None:
        app_module = self._import_fresh_app_module()

        response = app_module.app.test_client().post("/api/run-all")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(app_module.worker_launcher.calls, ["run-all"])
        self.assertEqual(
            response.get_json(),
            {"request_id": "batch-1", "pid": 123, "status": "submitted"},
        )

    def test_api_single_submits_finite_worker(self) -> None:
        app_module = self._import_fresh_app_module()

        response = app_module.app.test_client().post("/api/run/christies")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(app_module.worker_launcher.calls, ["christies"])
        self.assertEqual(response.get_json()["status"], "submitted")

    def test_api_rejects_lostart_from_dashboard_scope(self) -> None:
        app_module = self._import_fresh_app_module()

        response = app_module.app.test_client().post("/api/run/lostart")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(app_module.worker_launcher.calls, [])

    def test_api_reports_worker_launch_failure(self) -> None:
        app_module = self._import_fresh_app_module()
        app_module.worker_launcher.launch_all = lambda: (_ for _ in ()).throw(
            OSError("process limit")
        )

        response = app_module.app.test_client().post("/api/run-all")

        self.assertEqual(response.status_code, 503)
        self.assertIn("process limit", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
