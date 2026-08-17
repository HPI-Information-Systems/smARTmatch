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
            set(services),
            {"db", "scrapers", "matching_pipeline", "frontend"},
        )
        service = services["matching_pipeline"]
        self.assertEqual(service["build"]["dockerfile"], "matching_pipeline/Dockerfile")
        self.assertEqual(service["stop_grace_period"], "160s")
        self.assertEqual(service["gpus"], "all")
        self.assertEqual(
            [path.name for path in _ROOT.glob("docker-compose*.yml")],
            ["docker-compose.yml"],
        )

    def test_database_is_internal_only(self) -> None:
        compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())
        database = compose["services"]["db"]

        self.assertEqual(database["image"], "postgres:16.15")
        self.assertNotIn("ports", database)

    def test_frontend_uses_baked_code_and_read_only_data_mounts(self) -> None:
        compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())
        frontend = compose["services"]["frontend"]

        self.assertEqual(frontend["environment"]["SMARTMATCH_PROJECT_DIR"], "/project")
        self.assertEqual(
            frontend["volumes"],
            [
                {
                    "type": "bind",
                    "source": ".",
                    "target": "/project",
                    "read_only": True,
                    "bind": {"create_host_path": False},
                },
                "./db/images:/app/db/images:ro",
                "./cache:/app/cache:ro",
                "./logs:/app/logs",
            ],
        )
        dockerfile = (_ROOT / "frontend" / "Dockerfile").read_text()
        self.assertIn("COPY frontend /app/frontend", dockerfile)

    def test_python_images_install_component_runtime_requirements(self) -> None:
        dockerfile_copies = {
            "scrapers": "COPY scrapers/requirements.txt /tmp/requirements.txt",
            "frontend": "COPY frontend/requirements.txt /tmp/requirements.txt",
            "matching_pipeline": (
                "COPY matching_pipeline/requirements.txt /tmp/requirements.txt"
            ),
        }
        for component, copy_instruction in dockerfile_copies.items():
            dockerfile = (_ROOT / component / "Dockerfile").read_text()
            with self.subTest(component=component):
                self.assertIn(copy_instruction, dockerfile)

        self.assertEqual(
            _requirement_lines(_ROOT / "frontend" / "requirements.txt"),
            [
                "flask==3.1.0",
                "psycopg[binary]==3.2.2",
                "sqlalchemy==2.0.36",
                "waitress==3.0.2",
            ],
        )
        self.assertEqual(
            _requirement_lines(_ROOT / "scrapers" / "requirements.txt"),
            [
                "beautifulsoup4==4.14.2",
                "flask==3.1.0",
                "lxml==6.0.2",
                "pillow==12.3.0",
                "playwright==1.62.0",
                "psycopg[binary]==3.2.2",
                "requests==2.32.5",
                "sqlalchemy==2.0.36",
                "waitress==3.0.2",
                "werkzeug==3.1.8",
            ],
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
        self.assertIn("METADATA_GPU_MEMORY_UTILIZATION=0.55", development_env)
        self.assertIn("METADATA_MAX_NUM_SEQS=4", development_env)
        self.assertIn("SMARTMATCH_LOG_LEVEL=ALL", development_env)
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
