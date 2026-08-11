from __future__ import annotations

import unittest
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]


class ScraperDeploymentTests(unittest.TestCase):
    def test_one_scraper_service_uses_supervised_container(self) -> None:
        compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        scraper_services = [
            name for name in compose["services"] if name.startswith("scraper")
        ]

        self.assertEqual(scraper_services, ["scrapers"])
        healthcheck = compose["services"]["scrapers"]["healthcheck"]["test"][1]
        self.assertIn("status scraper-dashboard", healthcheck)
        self.assertIn("status scraper-scheduler", healthcheck)

        dockerfile = (_ROOT / "scrapers" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("supervisor", dockerfile)
        self.assertIn(
            'CMD ["supervisord", "-n", "-c", "/app/scrapers/supervisord.conf"]',
            dockerfile,
        )

    def test_supervisor_manages_dashboard_and_scheduler(self) -> None:
        config = (_ROOT / "scrapers" / "supervisord.conf").read_text(
            encoding="utf-8"
        )

        self.assertIn("[eventlistener:essential-process-fatal]", config)
        self.assertIn("[program:scraper-dashboard]", config)
        self.assertIn("command=python -m scraper_dashboard", config)
        self.assertIn("[program:scraper-scheduler]", config)
        self.assertIn("command=python -m scrapers.scheduler", config)


if __name__ == "__main__":
    unittest.main()
