"""Static contracts for persisted lost-artwork institution classification."""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_SQL = _ROOT / "db/init-production/01_schema_production.sql"
_INDICES_SQL = _ROOT / "db/init-production/02_indices.sql"
_MIGRATIONS_DIR = _ROOT / "db/init-production/migrations"
_REMOVED_INIT_SQL = (
    _ROOT / "db/init-production/04_lost_artwork_source_classification.sql"
)
_MIGRATION_SQL = _MIGRATIONS_DIR / "14_add_lost_artwork_institution_classification.sql"


class LostArtworkInstitutionClassificationTests(unittest.TestCase):
    def test_main_schema_has_a_plain_optional_institution_column(self) -> None:
        sql = _SCHEMA_SQL.read_text()
        self.assertFalse(_REMOVED_INIT_SQL.exists())
        self.assertIn("institution_classification text", sql)
        self.assertNotIn("classify_lost_artwork_institution", sql)
        self.assertNotIn("GENERATED ALWAYS AS", sql)
        self.assertNotIn("CREATE INDEX", sql)

    def test_fresh_database_indices_are_in_the_indices_file(self) -> None:
        sql = _INDICES_SQL.read_text()
        self.assertIn("idx_lost_artwork_institution_classification", sql)
        self.assertIn("idx_match_score_new_lost_id", sql)

    def test_existing_database_migration_is_generic(self) -> None:
        sql = _MIGRATION_SQL.read_text()
        self.assertIn("ADD COLUMN institution_classification text", sql)
        self.assertIn("idx_lost_artwork_institution_classification", sql)
        self.assertIn("idx_match_score_new_lost_id", sql)
        self.assertNotIn("GENERATED ALWAYS AS", sql)
        self.assertNotIn("CREATE OR REPLACE FUNCTION", sql)

    def test_shared_schema_and_migrations_have_no_spsg_logic(self) -> None:
        paths = [_SCHEMA_SQL, *_MIGRATIONS_DIR.glob("*.sql")]
        for path in paths:
            with self.subTest(path=path):
                sql = path.read_text().lower()
                self.assertNotIn("spsg", sql)
                self.assertNotIn("preuß", sql)
                self.assertNotIn("preuss", sql)


if __name__ == "__main__":
    unittest.main()
