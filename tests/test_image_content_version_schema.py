"""Contracts for versioning mutable image-file content."""

from __future__ import annotations

import unittest
from pathlib import Path

from scrapers.models_production import ImageFile

_ROOT = Path(__file__).resolve().parents[1]


class ImageContentVersionSchemaTests(unittest.TestCase):
    def test_fresh_schema_and_migration_define_version_trigger(self) -> None:
        schema = (_ROOT / "db/init-production/01_schema_production.sql").read_text()
        migration = (
            _ROOT / "db/init-production/migrations/24_version_image_file_content.sql"
        ).read_text()

        for sql in (schema, migration):
            self.assertIn("content_sha256", sql)
            self.assertIn("content_version", sql)
            self.assertIn("version_image_file_content_change", sql)
            self.assertIn("NEW.content_version = OLD.content_version + 1", sql)
            self.assertIn("NEW.is_embedded = false", sql)
            self.assertIn("BEFORE UPDATE OF content_sha256", sql)
            self.assertIn("image_matching_input_state", sql)
            self.assertIn("lost_content_revision", sql)
            self.assertIn("invalidate_all_image_matching_for_lost_change", sql)
            self.assertIn("invalidate_image_matching_after_lost_link_change", sql)
            self.assertIn("AFTER INSERT OR DELETE OR UPDATE", sql)
            self.assertIn("invalidate_image_matching_after_content_change", sql)
            self.assertIn("AFTER UPDATE OF content_sha256", sql)
            self.assertIn("SET lost_content_revision = lost_content_revision + 1", sql)
            self.assertIn("smartmatch.lost_invalidation_done", sql)
            self.assertIn("is_image_matching_completed_without_error = false", sql)
            self.assertIn("image.cleaned_up_at IS NULL", sql)
            self.assertIn("image.file_path IS NOT NULL", sql)
            self.assertIn("UPDATE match_score", sql)
            self.assertIn("DELETE FROM match_score", sql)
            self.assertIn("WHERE metadata_final_score IS NULL", sql)
            self.assertIn("auction_id = ANY(affected_auction_ids)", sql)
        self.assertIn("WHERE content_sha256 IS NULL", migration)

    def test_migration_preserves_scores_during_initial_replay(self) -> None:
        migration = (
            _ROOT / "db/init-production/migrations/24_version_image_file_content.sql"
        ).read_text()
        replay = migration.split(
            "-- Existing rows have no trustworthy digest.", maxsplit=1
        )[1]

        self.assertNotIn("UPDATE match_score", replay)
        self.assertNotIn("DELETE FROM match_score", replay)
        self.assertIn("UPDATE image_file", replay)
        self.assertIn("SET is_embedded = false", replay)
        self.assertIn("UPDATE auction_artwork_image_file", replay)
        self.assertIn("is_image_matching_processed = false", replay)
        self.assertIn("UPDATE auction_artwork artwork", replay)

    def test_generated_model_exposes_content_identity(self) -> None:
        self.assertIn("content_sha256", ImageFile.__table__.columns)
        self.assertIn("content_version", ImageFile.__table__.columns)
        self.assertFalse(ImageFile.__table__.columns.content_version.nullable)

    def test_scraper_handoff_carries_downloaded_digest(self) -> None:
        base = (_ROOT / "scrapers/utils/scraper.py").read_text()
        auction = (_ROOT / "scrapers/utils/auction_scraper.py").read_text()
        lost = (_ROOT / "scrapers/lostart/scraper.py").read_text()
        interface = (_ROOT / "scrapers/db_interface.py").read_text()

        self.assertIn("last_downloaded_image_content_sha256", base)
        self.assertIn(
            "image_content_sha256=self.last_downloaded_image_content_sha256",
            auction,
        )
        self.assertIn(
            "image_content_sha256=self.last_downloaded_image_content_sha256",
            lost,
        )
        self.assertIn("content_sha256 = :content_sha256", interface)
        self.assertIn("pg_advisory_xact_lock", interface)


if __name__ == "__main__":
    unittest.main()
