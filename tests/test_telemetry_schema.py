"""Static deployment contracts for optional daily telemetry."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA = _ROOT / "db/init-production/01_schema_production.sql"
_MIGRATION = _ROOT / "db/init-production/migrations/16_add_telemetry_daily_attempt.sql"
_PAGINATION_MIGRATION = (
    _ROOT / "db/init-production/migrations/18_add_telemetry_sync_pagination.sql"
)
_PAYLOAD_BYTES_MIGRATION = (
    _ROOT / "db/init-production/migrations/22_expand_telemetry_payload_bytes.sql"
)


class TelemetrySchemaTests(unittest.TestCase):
    def test_fresh_schema_and_migration_define_daily_attempt_state(self) -> None:
        for path in (_SCHEMA, _MIGRATION):
            sql = path.read_text()
            with self.subTest(path=path):
                self.assertIn("telemetry_daily_attempt", sql)
                self.assertIn("attempt_date", sql)
                self.assertIn("payload_sha256", sql)
                self.assertIn("'started', 'sent', 'failed'", sql)

    def test_payload_byte_totals_use_bigint(self) -> None:
        schema = _SCHEMA.read_text()
        initial_migration = _MIGRATION.read_text()
        expansion = _PAYLOAD_BYTES_MIGRATION.read_text()
        self.assertIn("payload_bytes   bigint", schema)
        self.assertIn("payload_bytes   bigint", initial_migration)
        self.assertIn("ALTER COLUMN payload_bytes TYPE bigint", expansion)

    def test_paginated_sync_state_is_available_for_existing_databases(self) -> None:
        schema = _SCHEMA.read_text()
        migration = _PAGINATION_MIGRATION.read_text()
        for column in ("sync_id", "page_count", "pages_sent"):
            self.assertIn(column, schema)
            self.assertIn(column, migration)

    def test_pagination_constraints_match_across_installation_paths(self) -> None:
        for path in (_SCHEMA, _MIGRATION, _PAGINATION_MIGRATION):
            sql = " ".join(path.read_text().split())
            with self.subTest(path=path):
                self.assertIn(
                    "CONSTRAINT telemetry_daily_attempt_page_count_check "
                    "CHECK (page_count IS NULL OR page_count > 0)",
                    sql,
                )
                self.assertIn(
                    "CONSTRAINT telemetry_daily_attempt_pages_sent_check "
                    "CHECK (pages_sent IS NULL OR pages_sent >= 0)",
                    sql,
                )

    def test_telemetry_configuration_contract_is_documented(self) -> None:
        example = (_ROOT / ".env.example").read_text()
        self.assertIn("TELEMETRY_ENABLED=false", example)
        self.assertIn("complete historical match graph", " ".join(example.split()))
        readme = (_ROOT / "telemetry" / "README.md").read_text()
        self.assertIn("complete historical match graph", readme)
        self.assertIn("does **not** limit synchronization", readme)
        self.assertIn("latest_applied_migration", readme)
        for filename in (".env.example", ".env.docker"):
            value = (_ROOT / filename).read_text()
            with self.subTest(filename=filename):
                self.assertIn("TELEMETRY_ENDPOINT=", value)
                self.assertNotIn("TELEMETRY_MAX_PAYLOAD_BYTES", value)
                self.assertIn("TELEMETRY_AUTH_TOKEN=", value)
                self.assertIn("TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP=", value)

    def test_telemetry_logic_is_a_top_level_package(self) -> None:
        package = _ROOT / "telemetry"
        for filename in (
            "__init__.py",
            "telemetry.py",
            "telemetry_sync.py",
            "build_provenance.py",
            "Dockerfile",
            "requirements.txt",
            "README.md",
        ):
            self.assertTrue((package / filename).is_file(), filename)
        self.assertFalse(
            (_ROOT / "matching_pipeline" / "shared" / "telemetry.py").exists()
        )
        self.assertFalse(
            (_ROOT / "matching_pipeline" / "shared" / "telemetry_sync.py").exists()
        )

    def test_telemetry_python_modules_stay_below_400_lines(self) -> None:
        paths = sorted(
            [
                *(_ROOT / "telemetry").glob("*.py"),
                *(_ROOT / "tests/telemetry").glob("*.py"),
            ]
        )
        line_counts = {
            str(path.relative_to(_ROOT)): len(path.read_text().splitlines())
            for path in paths
        }
        oversized = {
            path: line_count
            for path, line_count in line_counts.items()
            if line_count > 400
        }
        self.assertEqual(oversized, {})

    def test_telemetry_container_uses_baked_build_provenance(self) -> None:
        compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())
        service = compose["services"]["telemetry"]
        self.assertNotIn("SMARTMATCH_PROJECT_DIR", service["environment"])
        self.assertFalse(any(".git" in volume for volume in service["volumes"]))
        self.assertIn("./db/images:/app/db/images:ro", service["volumes"])
        self.assertNotIn("matching_pipeline", service.get("depends_on", {}))
        self.assertNotIn("args", service["build"])

        dockerfile = (_ROOT / "telemetry/Dockerfile").read_text()
        self.assertIn("FROM application AS provenance", dockerfile)
        self.assertIn("python -m telemetry.build_provenance", dockerfile)
        self.assertIn("COPY --from=provenance", dockerfile)
        self.assertIn("SMARTMATCH_BUILD_PROVENANCE_FILE", dockerfile)
        self.assertIn("SMARTMATCH_REQUIRE_BUILD_PROVENANCE=true", dockerfile)
        self.assertIn("COPY .git /tmp/repository/.git", dockerfile)
        self.assertIn("--git-dir /tmp/repository/.git", dockerfile)
        self.assertNotIn("ARG SMARTMATCH_BUILD_GIT_COMMIT", dockerfile)
        self.assertNotIn("apt-get install", dockerfile)

        dockerignore = (_ROOT / ".dockerignore").read_text()
        self.assertIn(".git/*\n", dockerignore)
        self.assertIn("!.git/HEAD\n", dockerignore)
        self.assertIn("!.git/packed-refs\n", dockerignore)
        self.assertIn("!.git/refs/**\n", dockerignore)
        self.assertNotIn("!.git/config", dockerignore)
        self.assertNotIn("!.git/objects", dockerignore)

    def test_metadata_upserts_refresh_the_metadata_event_date(self) -> None:
        matcher = (
            _ROOT
            / "matching_pipeline/metadata_matching/main_matcher/metadata_matcher.py"
        ).read_text()
        self.assertIn("metadata_match_date = now()", matcher)


if __name__ == "__main__":
    unittest.main()
