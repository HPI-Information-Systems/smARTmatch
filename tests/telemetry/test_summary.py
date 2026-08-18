"""Focused offline telemetry tests."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from telemetry import summary as telemetry


class SnapshotCollectionTests(unittest.TestCase):
    def test_image_snapshot_uses_path_and_size_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            images = root / "images"
            images.mkdir()
            (images / "sample.jpg").write_bytes(b"image bytes are not hashed")
            settings = telemetry.TelemetrySettings(
                endpoint="https://example.test/collect",
                auth_token="unit-test-static-bearer-token",
                image_root=images,
                timeout_seconds=3,
                match_expiration_seconds=30 * 86400,
            )
            database_snapshot = {
                "lost_artwork_hash": {"sha256": "a" * 64},
                "dependency_hashes": {},
                "counts": {},
                "match_categories": {},
                "scraper_dates": {},
                "matching_programs": [],
                "latest_applied_migration": {
                    "application_order": 22,
                    "migration_name": "22_expand_telemetry_payload_bytes.sql",
                    "checksum_sha256": "a" * 64,
                    "applied_at": "2026-08-16T12:30:00Z",
                },
            }
            with mock.patch.object(
                telemetry,
                "_collect_database_snapshot",
                return_value=database_snapshot,
            ), mock.patch.object(
                telemetry,
                "_git_identity",
                return_value={},
            ), mock.patch.object(
                telemetry,
                "_runtime_reproducibility_metadata",
                return_value={},
            ), mock.patch.object(
                telemetry,
                "hash_directory_tree",
                wraps=telemetry.hash_directory_tree,
            ) as tree_hash:
                payload = telemetry.collect_telemetry_payload(
                    settings,
                    generated_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
                    trigger="startup",
                )

        self.assertEqual(payload["schema_version"], 2)
        self.assertNotIn("recent_matches", payload)
        self.assertEqual(
            payload["datasets"]["images"]["hash_basis"],
            "relative_path_and_size",
        )
        self.assertFalse(tree_hash.call_args.kwargs["hash_file_contents"])
        self.assertEqual(tree_hash.call_args.kwargs["retain_file_paths"], set())
        self.assertEqual(
            payload["reproducibility"]["database"]["latest_applied_migration"][
                "migration_name"
            ],
            "22_expand_telemetry_payload_bytes.sql",
        )
