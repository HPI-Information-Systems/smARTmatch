"""Contracts for retaining scraper image source URLs for telemetry replication."""

from __future__ import annotations

import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


class ImageSourceUrlSchemaTests(unittest.TestCase):
    def test_fresh_schema_and_migration_add_source_url(self) -> None:
        schema = (_ROOT / "db/init-production/01_schema_production.sql").read_text()
        migration = (
            _ROOT / "db/init-production/migrations/17_add_image_file_source_url.sql"
        ).read_text()
        uniqueness_migration = (
            _ROOT / "db/init-production/migrations/19_make_image_file_path_unique.sql"
        ).read_text()
        cleanup_migration = (
            _ROOT / "db/init-production/migrations/21_mark_cleaned_up_image_files.sql"
        ).read_text()
        self.assertIn("file_path text UNIQUE", schema)
        self.assertIn("source_url text", schema)
        self.assertIn("ADD COLUMN IF NOT EXISTS source_url text", migration)
        self.assertIn(
            "CREATE UNIQUE INDEX uq_image_file_file_path",
            uniqueness_migration,
        )
        self.assertIn("WHERE file_path IS NOT NULL", uniqueness_migration)
        self.assertIn("FIRST_VALUE(image_file_id)", uniqueness_migration)
        self.assertIn("ORDER BY image_file_id", uniqueness_migration)
        self.assertNotIn("MIN(image_file_id)", uniqueness_migration)
        self.assertIn("auction_image_file_id", uniqueness_migration)
        self.assertIn("lost_image_file_id", uniqueness_migration)
        self.assertIn("jsonb_set", uniqueness_migration)
        self.assertIn("ALTER COLUMN file_path DROP NOT NULL", cleanup_migration)
        self.assertIn("ADD COLUMN IF NOT EXISTS cleaned_up_at", cleanup_migration)

    def test_duplicate_image_migration_invalidates_id_keyed_embedding_state(
        self,
    ) -> None:
        migration = (
            _ROOT / "db/init-production/migrations/19_make_image_file_path_unique.sql"
        ).read_text()

        self.assertIn("CREATE TEMP TABLE image_file_duplicate_canonical", migration)
        self.assertIn("HAVING COUNT(*) > 1", migration)
        self.assertNotIn("BOOL_OR(image.is_embedded)", migration)
        self.assertRegex(
            migration,
            r"UPDATE image_file canonical\s+SET is_embedded = false",
        )
        self.assertRegex(
            migration,
            r"UPDATE auction_artwork_image_file link\s+"
            r"SET is_image_matching_processed = false",
        )
        self.assertRegex(
            migration,
            r"UPDATE auction_artwork artwork\s+"
            r"SET is_image_matching_processed = false,\s+"
            r"is_image_matching_processed_at = NULL",
        )
        reset_position = migration.index("SET is_embedded = false")
        delete_position = migration.index("DELETE FROM image_file image")
        unique_position = migration.index("CREATE UNIQUE INDEX")
        self.assertLess(reset_position, delete_position)
        self.assertLess(delete_position, unique_position)
        self.assertIn(
            "duplicate image-file embedding state was not fully invalidated",
            migration,
        )

    def test_auction_images_use_url_hashes_to_avoid_stale_cache_aliases(self) -> None:
        source = (_ROOT / "scrapers/utils/auction_scraper.py").read_text()
        self.assertIn("include_url_hash=True", source)

    def test_auction_scrapers_use_failure_safe_image_reconciliation(self) -> None:
        base_source = (_ROOT / "scrapers/utils/auction_scraper.py").read_text()
        self.assertIn(
            "image_source_urls=self.last_downloaded_image_sources", base_source
        )
        self.assertIn("authoritative=self.last_image_download_complete", base_source)

        paths = (
            "scrapers/christies/scraper.py",
            "scrapers/dorotheum/scraper.py",
            "scrapers/drouot/scraper.py",
            "scrapers/lottissimo/scraper.py",
            "scrapers/sothebys/scraper.py",
        )
        for relative in paths:
            with self.subTest(path=relative):
                source = (_ROOT / relative).read_text()
                self.assertIn("self.set_lot_images(", source)

    def test_lost_art_scraper_persists_download_source_mapping(self) -> None:
        source = (_ROOT / "scrapers/lostart/scraper.py").read_text()
        self.assertIn("image_source_urls=self.last_downloaded_image_sources", source)


if __name__ == "__main__":
    unittest.main()
