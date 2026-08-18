"""Focused selective synchronization tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telemetry import sync_catalog as sync


class _Settings:
    endpoint = "https://receiver.example/api/telemetry/sync/v3/pages"
    timeout_seconds = 10.0
    auth_token = "unit-test-static-bearer-token"


class SyncSelectionValidationTests(unittest.TestCase):
    def test_receiver_cannot_request_ids_outside_advertised_inventory(self) -> None:
        inventory = {
            "match_score": {},
            "lost_artwork": {},
            "auction_artwork": {},
        }
        with self.assertRaisesRegex(ValueError, "outside the inventory"):
            sync._validate_needed_acknowledgement(
                {
                    "match_score": [],
                    "lost_artwork": ["unadvertised-id"],
                    "auction_artwork": [],
                },
                inventory,
            )

    def test_falsey_malformed_acknowledgement_fields_are_rejected(self) -> None:
        for needed in (
            [],
            {"match_score": {}},
            {"lost_artwork": {}},
            {"auction_artwork": {}},
        ):
            with self.subTest(needed=needed):
                with self.assertRaises((ValueError, sync.SyncProtocolError)):
                    sync._parse_needed_identifiers(needed)


class SyncCatalogTests(unittest.TestCase):
    def _connection(self):
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        return connection, cursor

    def test_final_acknowledgement_can_reference_an_earlier_page_on_disk(self) -> None:
        lost_id = "10000000-0000-4000-8000-000000000001"
        auction_id = "20000000-0000-4000-8000-000000000001"
        inventory = {
            "match_score": {
                f"{lost_id}:{auction_id}": {
                    "lost_id": lost_id,
                    "auction_id": auction_id,
                    "sha256": "a" * 64,
                }
            },
            "lost_artwork": {lost_id: "b" * 64},
            "auction_artwork": {auction_id: "c" * 64},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = sync.SyncCatalog(Path(temp_dir) / "catalog.sqlite3")
            try:
                catalog.record_inventory(inventory)
                catalog.record_needed(
                    {
                        "match_score": [{"lost_id": lost_id, "auction_id": auction_id}],
                        "lost_artwork": [],
                        "auction_artwork": [],
                    },
                    page_inventory=None,
                )
                self.assertEqual(catalog.requested_counts(), (1, 1, 1))
                indexes = {
                    row[1]
                    for row in catalog._conn.execute(
                        "PRAGMA index_list('requested_match')"
                    ).fetchall()
                }
                self.assertIn("requested_match_auction_id", indexes)
            finally:
                catalog.close()

    def test_changed_hash_between_snapshots_is_rejected(self) -> None:
        lost_id = "10000000-0000-4000-8000-000000000001"
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = sync.SyncCatalog(Path(temp_dir) / "catalog.sqlite3")
            try:
                catalog.record_inventory(
                    {
                        "match_score": {},
                        "lost_artwork": {lost_id: "a" * 64},
                        "auction_artwork": {},
                    }
                )
                with self.assertRaises(sync.SourceSnapshotChanged):
                    catalog.verify_content(
                        {
                            "hashes": {
                                "match_score": {},
                                "lost_artwork": {lost_id: "b" * 64},
                                "auction_artwork": {},
                            }
                        },
                        expected_matches=set(),
                        expected_lost={lost_id},
                        expected_auction=set(),
                    )
            finally:
                catalog.close()
