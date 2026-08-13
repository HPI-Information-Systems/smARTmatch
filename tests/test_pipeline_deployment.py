"""Static checks for the combined matching-pipeline deployment."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_LIGHTGLUE_COMMIT = "eb42fee2d71449efb0aa5c10549752b5d75384d8"


class PipelineDeploymentTests(unittest.TestCase):
    def test_compose_uses_only_the_combined_matching_service(self) -> None:
        compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())
        services = compose["services"]
        self.assertEqual(
            set(services), {"db", "scrapers", "matching_pipeline", "frontend"}
        )
        service = services["matching_pipeline"]
        self.assertEqual(
            service["build"]["dockerfile"], "matching_pipeline/Dockerfile"
        )
        self.assertEqual(service["stop_grace_period"], "160s")
        self.assertEqual(service["gpus"], "all")
        self.assertEqual(
            [path.name for path in _ROOT.glob("docker-compose*.yml")],
            ["docker-compose.yml"],
        )

    def test_compose_environment_file_is_overridable_and_secret_free(self) -> None:
        compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())
        expected = ["${SMARTMATCH_ENV_FILE:-./.env.docker}"]
        for service in compose["services"].values():
            self.assertEqual(service["env_file"], expected)

        development_env = (_ROOT / ".env.docker").read_text()
        self.assertIn("HF_TOKEN=\n", development_env)
        self.assertIn("ALLOW_NON_GPU_INFERENCE=0", development_env)
        self.assertIn("METADATA_BACKEND=vllm", development_env)
        self.assertIn("METADATA_DEVICE=cuda", development_env)
        self.assertIn("SMARTMATCH_LOG_LEVEL=ERROR", development_env)
        self.assertIn("SMARTMATCH_LOG_RETENTION_DAYS=30", development_env)

    def test_pipeline_dockerfile_pins_base_and_lightglue(self) -> None:
        dockerfile = (_ROOT / "matching_pipeline" / "Dockerfile").read_text()
        first_line = dockerfile.splitlines()[0]
        self.assertIn("python:3.12.7-slim@sha256:", first_line)
        self.assertIn(f"LightGlue@{_LIGHTGLUE_COMMIT}", dockerfile)
        self.assertIn("--no-deps", dockerfile)
        self.assertIn("run_pipeline_scheduler.py", dockerfile)

    def test_combined_requirements_are_pinned_without_opencv_conflict(self) -> None:
        for filename in ("requirements.in", "requirements.txt"):
            requirements = _requirement_lines(_ROOT / "matching_pipeline" / filename)
            unpinned = [line for line in requirements if "==" not in line]
            self.assertEqual(unpinned, [], filename)
        lock = (_ROOT / "matching_pipeline" / "requirements.txt").read_text()
        self.assertIn("opencv-python-headless==4.13.0.92", lock)
        self.assertNotIn("\nopencv-python==", lock)


def _requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


if __name__ == "__main__":
    unittest.main()
