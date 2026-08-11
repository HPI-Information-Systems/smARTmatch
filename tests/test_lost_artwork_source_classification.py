"""Static contract tests for lost-artwork source classification SQL."""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_INIT_SQL = _ROOT / "db/init-production/04_lost_artwork_source_classification.sql"
_MIGRATION_SQL = (
    _ROOT
    / "db/init-production/migrations/14_add_lost_artwork_source_classification_view.sql"
)


class LostArtworkSourceClassificationTests(unittest.TestCase):
    def test_fresh_database_and_migration_use_identical_view_definition(self) -> None:
        self.assertEqual(_INIT_SQL.read_text(), _MIGRATION_SQL.read_text())

    def test_view_exposes_all_agreed_categories_in_precedence_order(self) -> None:
        sql = _INIT_SQL.read_text()
        categories = [
            "SPSG (internal and lostart)",
            "SPSG (internal)",
            "SPSG (lostart)",
            "non-SPSG",
        ]
        positions = [sql.index(f"THEN '{category}'") for category in categories[:-1]]
        positions.append(sql.index(f"ELSE '{categories[-1]}'"))
        self.assertEqual(positions, sorted(positions))

    def test_reporter_detection_is_restricted_to_identity_fields(self) -> None:
        sql = _INIT_SQL.read_text()
        for field in (
            "Kontakt",
            "Contact",
            "Suchauftrag, Institution",
            "Search Request, Institution",
            "E-Mail",
            "Homepage",
        ):
            self.assertIn(f"->> '{field}'", sql)

        evidence_cte = sql.split("), flags AS (", maxsplit=1)[0]
        for misleading_field in ("Beschreibung", "description", "Provenienz", "Literatur"):
            self.assertNotIn(f"->> '{misleading_field}'", evidence_cte)

        self.assertNotIn("JOIN public.institution", sql)
        self.assertNotIn("la.institution_id", sql)

    def test_migration_helper_defaults_to_latest_migration(self) -> None:
        helper = (_ROOT / "scripts/apply_production_migration.sh").read_text()
        self.assertIn(
            "migrations/14_add_lost_artwork_source_classification_view.sql", helper
        )


if __name__ == "__main__":
    unittest.main()
