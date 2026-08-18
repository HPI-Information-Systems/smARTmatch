from __future__ import annotations

import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_INIT_SCHEMA = _ROOT / "db/init-production/01_schema_production.sql"
_MIGRATION = _ROOT / "db/init-production/migrations/15_add_scraper_queue_progress.sql"


class ScraperProgressSchemaTests(unittest.TestCase):
    def test_fresh_schema_and_existing_database_migration_define_progress(self) -> None:
        init_sql = _INIT_SCHEMA.read_text()
        migration_sql = _MIGRATION.read_text()

        for column in ("queue_total", "queue_processed", "progress_updated_at"):
            self.assertIn(column, init_sql)
            self.assertIn(column, migration_sql)

        self.assertIn("ALTER TABLE public.scraper_run", migration_sql)
        self.assertIn("SET NOT NULL", migration_sql)


if __name__ == "__main__":
    unittest.main()
