from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "db/init-production/01_schema_production.sql"
MIGRATION_PATH = (
    ROOT
    / "db/init-production/migrations/23_reset_parent_for_pending_auction_image.sql"
)


def _assert_pending_link_trigger(sql: str) -> None:
    assert "reset_auction_artwork_image_matching_for_pending_link" in sql
    assert re.search(
        r"AFTER INSERT ON auction_artwork_image_file\s+"
        r"FOR EACH ROW\s+"
        r"WHEN \(\s*"
        r"NEW\.is_image_matching_processed = false\s+"
        r"OR NEW\.is_image_matching_completed_without_error = false",
        sql,
    )
    assert re.search(
        r"UPDATE auction_artwork\s+"
        r"SET is_image_matching_processed = false,\s+"
        r"is_image_matching_processed_at = NULL\s+"
        r"WHERE auction_artwork_id = NEW\.auction_artwork_id",
        sql,
    )


def test_fresh_schema_resets_parent_when_a_pending_image_link_is_inserted() -> None:
    _assert_pending_link_trigger(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_migration_installs_trigger_and_repairs_existing_contradictions() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8")

    _assert_pending_link_trigger(migration)
    assert re.search(
        r"UPDATE auction_artwork artwork\s+"
        r"SET is_image_matching_processed = false,\s+"
        r"is_image_matching_processed_at = NULL",
        migration,
    )
    assert "FROM auction_artwork_image_file link" in migration
    assert "link.is_image_matching_processed = false" in migration
    assert "link.is_image_matching_completed_without_error = false" in migration
