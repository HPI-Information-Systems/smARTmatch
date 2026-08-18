"""Focused selective synchronization tests."""

from __future__ import annotations

import gzip
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from telemetry import sync_codec as sync
from telemetry import sync_constants


class _Settings:
    endpoint = "https://receiver.example/api/telemetry/sync/v3/pages"
    timeout_seconds = 10.0
    auth_token = "unit-test-static-bearer-token"


class SyncEncodingTests(unittest.TestCase):
    def test_page_encoding_is_bounded_and_deterministic(self) -> None:
        envelope = {
            "schema_version": 3,
            "operation": {"sync_id": "id"},
            "page": {"phase": "data", "number": 0},
            "entities": {
                "lost_artwork": {
                    "lost-id": {
                        "lost_artwork_id": "lost-id",
                        "title": "Complete title",
                        "raw_data": {"nested": "value"},
                    }
                },
                "auction_artwork": {
                    "auction-id": {
                        "auction_artwork_id": "auction-id",
                        "description": "Complete description",
                    }
                },
            },
            "rows": {"match_score": []},
        }
        first = sync.encode_sync_page(envelope)
        second = sync.encode_sync_page(envelope)
        self.assertEqual(first, second)
        self.assertEqual(json.loads(gzip.decompress(first.body)), envelope)

    def test_final_manifest_uses_constant_size_rolling_hash(self) -> None:
        pages = [
            sync.RawPage(Path(f"page-{index}.json"), f"{index:064x}", {})
            for index in range(100)
        ]
        envelope = sync._page_envelope(
            sync_id="sync-id",
            trigger="daily",
            generated_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
            phase="data",
            page_number=len(pages) - 1,
            pages=pages,
            content={"entities": {}, "rows": {}, "hashes": {}},
            summary=None,
            operation_hash=sync._operation_hash(pages),
        )
        manifest = envelope["manifest"]
        self.assertNotIn("ordered_page_content_sha256", manifest)
        self.assertEqual(manifest["operation_sha256"], sync._operation_hash(pages))

    def test_inventory_page_size_bounds_worst_case_acknowledgement(self) -> None:
        lost_ids = [
            f"10000000-0000-4000-8000-{index:012x}"
            for index in range(sync_constants.INVENTORY_MATCHES_PER_PAGE)
        ]
        auction_ids = [
            f"20000000-0000-4000-8000-{index:012x}"
            for index in range(sync_constants.INVENTORY_MATCHES_PER_PAGE)
        ]
        acknowledgement = {
            "sync_id": "30000000-0000-4000-8000-000000000001",
            "phase": "inventory",
            "page_number": 0,
            "payload_sha256": "a" * 64,
            "replayed": False,
            "complete": False,
            "needed": {
                "match_score": [
                    {"lost_id": lost_id, "auction_id": auction_id}
                    for lost_id, auction_id in zip(lost_ids, auction_ids, strict=True)
                ],
                "lost_artwork": lost_ids,
                "auction_artwork": auction_ids,
            },
        }

        self.assertEqual(sync_constants.INVENTORY_MATCHES_PER_PAGE, 1000)
        self.assertLessEqual(
            len(sync._canonical_json(acknowledgement)),
            sync_constants.MAX_ACKNOWLEDGEMENT_BYTES,
        )
