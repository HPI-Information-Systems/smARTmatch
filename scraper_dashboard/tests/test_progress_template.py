from __future__ import annotations

import unittest
from pathlib import Path

from flask import Flask, render_template


_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"
_TEMPLATE_PATH = _TEMPLATE_DIR / "dashboard.html"


class DashboardProgressTemplateTests(unittest.TestCase):
    def test_unknown_progress_renders_without_none_percentage(self) -> None:
        app = Flask(__name__, template_folder=str(_TEMPLATE_DIR))
        scraper = {
            "name": "christies",
            "display_name": "Christie's",
            "is_running": True,
            "total_entries": 25,
            "progress": {
                "urls_total": 0,
                "urls_processed": 0,
                "progress_percent": None,
                "elapsed_seconds": 2,
                "eta_seconds": None,
            },
            "last_run": None,
        }

        with app.test_request_context("/"):
            html = render_template("dashboard.html", scrapers=[scraper])

        self.assertIn('style="width: 0%;"', html)
        self.assertIn("discovering total...", html)
        self.assertNotIn("None%", html)

    def test_status_polling_uses_one_serialized_refresh_loop(self) -> None:
        source = _TEMPLATE_PATH.read_text()

        self.assertNotIn("pollingIntervals", source)
        self.assertIn("statusRefreshInFlight", source)
        self.assertEqual(source.count("setInterval("), 1)


if __name__ == "__main__":
    unittest.main()
