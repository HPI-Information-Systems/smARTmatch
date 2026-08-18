"""Focused selective synchronization tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telemetry import sync_budget as sync
from telemetry import sync_catalog, sync_codec, sync_constants


class _Settings:
    endpoint = "https://receiver.example/api/telemetry/sync/v3/pages"
    timeout_seconds = 10.0
    auth_token = "unit-test-static-bearer-token"


class SyncBudgetTests(unittest.TestCase):
    def test_workspace_growth_does_not_rescan_the_spool_tree(self) -> None:
        original_directory_size = sync._directory_size
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            sync,
            "_directory_size",
            wraps=original_directory_size,
        ) as directory_size:
            root = Path(temp_dir)
            pages: list[sync_codec.RawPage] = []
            budget = sync.WorkspaceBudget(root, max_pages=100)

            for index in range(40):
                budget.next_page_materialization_limit(
                    sync_constants.TARGET_UNCOMPRESSED_PAGE_BYTES
                )
                sync_codec._write_raw_page(
                    root,
                    {"page": index},
                    pages,
                    {},
                    raw=f"page-{index}".encode(),
                    budget=budget,
                )

            self.assertEqual(directory_size.call_count, 1)

    def test_catalog_growth_is_incrementally_accounted(self) -> None:
        lost_id = "10000000-0000-4000-8000-000000000001"
        auction_id = "20000000-0000-4000-8000-000000000001"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            budget = sync.WorkspaceBudget(root)
            catalog = sync_catalog.SyncCatalog(root / "catalog.sqlite3", budget=budget)
            try:
                catalog.record_inventory(
                    {
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
                )

                self.assertEqual(
                    budget._used_bytes,
                    catalog.path.stat().st_size,
                )
            finally:
                catalog.close()

    def test_workspace_budget_bounds_pages_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pages: list[sync_codec.RawPage] = []
            budget = sync.WorkspaceBudget(root, max_bytes=100, max_pages=1)
            sync_codec._write_raw_page(
                root,
                {"value": 1},
                pages,
                {},
                raw=b"small",
                budget=budget,
            )
            with self.assertRaisesRegex(sync.SyncWorkspaceLimitError, "pages"):
                sync_codec._write_raw_page(
                    root,
                    {"value": 2},
                    pages,
                    {},
                    raw=b"second",
                    budget=budget,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            budget = sync.WorkspaceBudget(root, max_bytes=4, max_pages=2)
            with self.assertRaisesRegex(sync.SyncWorkspaceLimitError, "bytes"):
                sync_codec._write_raw_page(
                    root,
                    {"value": 1},
                    [],
                    {},
                    raw=b"12345",
                    budget=budget,
                )

    def test_aggregate_transfer_budget_is_bounded(self) -> None:
        sync_codec._check_transfer_budget(sync_constants.MAX_SYNC_TRANSFER_BYTES)
        with self.assertRaises(sync.SyncWorkspaceLimitError):
            sync_codec._check_transfer_budget(
                sync_constants.MAX_SYNC_TRANSFER_BYTES + 1
            )
