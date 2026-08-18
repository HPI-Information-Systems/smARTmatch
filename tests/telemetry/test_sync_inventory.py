"""Focused selective synchronization tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telemetry import sync_constants
from telemetry import sync_inventory as sync
from telemetry.sync_errors import SyncWorkspaceLimitError


class _Settings:
    endpoint = "https://receiver.example/api/telemetry/sync/v3/pages"
    timeout_seconds = 10.0
    auth_token = "unit-test-static-bearer-token"


class SyncInventoryTests(unittest.TestCase):
    def test_inventory_deduplicates_artwork_hashes_per_page(self) -> None:
        rows = [
            {
                "lost_id": "lost-1",
                "auction_id": f"auction-{index}",
                "match_score": {"lost_id": "lost-1", "auction_id": f"auction-{index}"},
                "lost_artwork": {"lost_artwork_id": "lost-1", "title": "Lost"},
                "auction_artwork": {
                    "auction_artwork_id": f"auction-{index}",
                    "title": f"Auction {index}",
                },
            }
            for index in range(2)
        ]
        graph_hashes = {
            "hashes": {
                "match_score": {
                    f"lost-1:auction-{index}": f"match-{index}" for index in range(2)
                },
                "lost_artwork": {"lost-1": "lost-hash"},
                "auction_artwork": {
                    f"auction-{index}": f"auction-{index}-hash" for index in range(2)
                },
            }
        }
        with tempfile_directory() as directory, mock.patch.object(
            sync, "_snapshot_connection"
        ) as connection, mock.patch.object(
            sync, "_fetch_inventory_rows", side_effect=[rows, []]
        ), mock.patch.object(
            sync,
            "_fetch_requested_match_rows",
            return_value=[row["match_score"] for row in rows],
        ), mock.patch.object(
            sync, "_build_data_content", return_value=graph_hashes
        ):
            fake_conn = connection.return_value
            pages = sync._spool_inventory_pages(directory)
            fake_conn.rollback.assert_called_once()
            payload = json.loads(pages[0].path.read_text())
            self.assertEqual(len(payload["inventory"]["match_score"]), 2)
            self.assertEqual(payload["inventory"]["lost_artwork"].keys(), {"lost-1"})
            self.assertEqual(len(payload["inventory"]["auction_artwork"]), 2)


class SyncInventoryResourceLimitTests(unittest.TestCase):
    def test_inventory_reserves_capacity_for_a_data_page_before_closure_loading(
        self,
    ) -> None:
        rows = [
            {
                "lost_id": "10000000-0000-4000-8000-000000000001",
                "auction_id": "20000000-0000-4000-8000-000000000001",
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            sync, "_fetch_inventory_rows", side_effect=[rows, []]
        ), mock.patch.object(sync, "_fetch_requested_match_rows") as fetch_rows:
            root = Path(temp_dir)
            with self.assertRaisesRegex(SyncWorkspaceLimitError, "pages"):
                sync._spool_inventory_pages(
                    root,
                    conn=mock.Mock(),
                    budget=sync.WorkspaceBudget(root, max_pages=1),
                )

        fetch_rows.assert_not_called()

    def test_inventory_workspace_is_checked_before_complete_rows_are_loaded(
        self,
    ) -> None:
        rows = [
            {
                "lost_id": "10000000-0000-4000-8000-000000000001",
                "auction_id": "20000000-0000-4000-8000-000000000001",
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            sync, "_fetch_inventory_rows", side_effect=[rows, []]
        ), mock.patch.object(sync, "_fetch_requested_match_rows") as fetch_rows:
            root = Path(temp_dir)
            (root / "used").write_bytes(b"x")
            budget = sync.WorkspaceBudget(
                root,
                max_bytes=(sync_constants._MATERIALIZATION_FIXED_OVERHEAD_BYTES + 1),
            )
            with self.assertRaisesRegex(SyncWorkspaceLimitError, "capacity"):
                sync._spool_inventory_pages(
                    root / "inventory",
                    conn=mock.Mock(),
                    budget=budget,
                )

        fetch_rows.assert_not_called()

    def test_single_oversized_inventory_closure_is_not_materialized(self) -> None:
        lost_id = "10000000-0000-4000-8000-000000000001"
        auction_id = "20000000-0000-4000-8000-000000000001"
        rows = [{"lost_id": lost_id, "auction_id": auction_id}]
        match_rows = [{"lost_id": lost_id, "auction_id": auction_id}]
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            sync, "_fetch_inventory_rows", side_effect=[rows, []]
        ), mock.patch.object(
            sync, "_fetch_requested_match_rows", return_value=match_rows
        ), mock.patch.object(
            sync,
            "_build_data_content",
            side_effect=sync._ClosureMaterializationLimit(
                label="lost_artwork_image_file rows",
                attempted_bytes=sync.MAX_UNCOMPRESSED_PAGE_BYTES,
                max_bytes=(
                    sync.MAX_UNCOMPRESSED_PAGE_BYTES - sync._PAGE_ENVELOPE_RESERVE_BYTES
                ),
            ),
        ):
            root = Path(temp_dir)
            with self.assertRaisesRegex(
                sync.UnsendableClosureError, "before closure materialization"
            ):
                sync._spool_inventory_pages(root, conn=mock.Mock())


class tempfile_directory:
    def __enter__(self) -> Path:
        import tempfile

        self._temporary = tempfile.TemporaryDirectory()
        return Path(self._temporary.name)

    def __exit__(self, *_args) -> None:
        self._temporary.cleanup()
