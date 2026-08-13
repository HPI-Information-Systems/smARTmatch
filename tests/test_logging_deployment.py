"""Static deployment checks for unified application logging."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_PYTHON_SERVICES = ("scrapers", "matching_pipeline", "frontend")


class LoggingDeploymentTests(unittest.TestCase):
    def test_python_services_mount_shared_daily_log_directory(self) -> None:
        compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())
        for service_name in _PYTHON_SERVICES:
            service = compose["services"][service_name]
            self.assertNotIn("container_name", service)
            self.assertEqual(
                service["environment"]["SMARTMATCH_CONTAINER_NAME"], service_name
            )
            self.assertIn("./logs:/app/logs", service["volumes"])
            self.assertEqual(service["logging"]["driver"], "json-file")
            self.assertEqual(service["logging"]["options"]["max-size"], "200m")
            self.assertEqual(service["logging"]["options"]["max-file"], "3")

        self.assertNotIn("SMARTMATCH_CONTAINER_NAME", compose["services"]["db"])

    def test_shared_adapter_is_in_the_docker_build_context(self) -> None:
        dockerignore = (_ROOT / ".dockerignore").read_text().splitlines()
        self.assertNotIn("shared/", dockerignore)
        self.assertTrue((_ROOT / "shared" / "logging_adapter.py").is_file())

    def test_python_images_copy_shared_adapter(self) -> None:
        for dockerfile_path in (
            _ROOT / "scrapers" / "Dockerfile",
            _ROOT / "matching_pipeline" / "Dockerfile",
            _ROOT / "frontend" / "Dockerfile",
        ):
            dockerfile = dockerfile_path.read_text()
            self.assertIn("PYTHONPATH=/app", dockerfile, dockerfile_path)
            self.assertIn("COPY shared /app/shared", dockerfile, dockerfile_path)
            self.assertIn("/app/logs", dockerfile, dockerfile_path)

    def test_docker_environment_exposes_only_unified_log_controls(self) -> None:
        environment = (_ROOT / ".env.docker").read_text()
        self.assertIn("SMARTMATCH_LOG_LEVEL=ERROR", environment)
        self.assertIn("SMARTMATCH_LOG_RETENTION_DAYS=30", environment)
        self.assertIn("SMARTMATCH_LOG_DIR=/app/logs", environment)
        self.assertNotIn("METADATA_MATCHER_LOG_LEVEL", environment)
        self.assertNotIn("METADATA_VLLM_VERBOSE", environment)


if __name__ == "__main__":
    unittest.main()
