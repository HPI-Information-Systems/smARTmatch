"""Focused selective synchronization tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telemetry import sync_data as sync
from telemetry.sync_errors import SyncWorkspaceLimitError


class _Settings:
    endpoint = "https://receiver.example/api/telemetry/sync/v3/pages"
    timeout_seconds = 10.0
    auth_token = "unit-test-static-bearer-token"


class SyncDataResourceLimitTests(unittest.TestCase):
    def test_oversized_batch_splits_before_full_closure_materialization(self) -> None:
        lost_ids = {
            "10000000-0000-4000-8000-000000000001",
            "10000000-0000-4000-8000-000000000002",
        }
        build_sizes = []

        def build_content(
            _conn,
            _match_rows,
            current_lost_ids,
            _auction_ids,
            *,
            materialization_budget,
        ):
            build_sizes.append(len(current_lost_ids))
            if len(current_lost_ids) > 1:
                raise sync._ClosureMaterializationLimit(
                    label="lost artwork links",
                    attempted_bytes=materialization_budget.max_bytes + 1,
                    max_bytes=materialization_budget.max_bytes,
                )
            entity_id = next(iter(current_lost_ids))
            return {
                "entities": {
                    "lost_artwork": {entity_id: {"lost_artwork_id": entity_id}},
                    "auction_artwork": {},
                },
                "rows": {"match_score": []},
                "hashes": {
                    "match_score": {},
                    "lost_artwork": {entity_id: "a" * 64},
                    "auction_artwork": {},
                },
            }

        catalog = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            sync, "_build_data_content", side_effect=build_content
        ):
            root = Path(temp_dir)
            pages: list[sync.RawPage] = []
            sync._spool_data_content(
                mock.Mock(),
                root,
                [],
                lost_ids,
                set(),
                pages,
                catalog=catalog,
                budget=sync.WorkspaceBudget(root),
                expected_matches=set(),
                expected_lost=lost_ids,
                expected_auction=set(),
            )

        self.assertEqual(build_sizes, [2, 1, 1])
        self.assertEqual(len(pages), 2)

    def test_page_quota_is_checked_before_requested_rows_are_loaded(self) -> None:
        lost_id = "10000000-0000-4000-8000-000000000001"
        auction_id = "20000000-0000-4000-8000-000000000001"
        catalog = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            sync, "_fetch_requested_match_rows"
        ) as fetch_rows:
            root = Path(temp_dir)
            with self.assertRaisesRegex(SyncWorkspaceLimitError, "pages"):
                sync._spool_requested_match_pairs(
                    mock.Mock(),
                    root,
                    [(lost_id, auction_id)],
                    [],
                    catalog=catalog,
                    budget=sync.WorkspaceBudget(root, max_pages=0),
                )

        fetch_rows.assert_not_called()

    def test_workspace_capacity_is_checked_before_requested_rows_are_loaded(
        self,
    ) -> None:
        lost_id = "10000000-0000-4000-8000-000000000001"
        auction_id = "20000000-0000-4000-8000-000000000001"
        catalog = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            sync, "_fetch_requested_match_rows"
        ) as fetch_rows:
            root = Path(temp_dir)
            (root / "full").write_bytes(b"12345")
            with self.assertRaisesRegex(SyncWorkspaceLimitError, "workspace"):
                sync._spool_requested_match_pairs(
                    mock.Mock(),
                    root,
                    [(lost_id, auction_id)],
                    [],
                    catalog=catalog,
                    budget=sync.WorkspaceBudget(root, max_bytes=5),
                )

        fetch_rows.assert_not_called()

    def test_incompressible_single_closure_is_rejected_while_spooling(self) -> None:
        content = {
            "entities": {"lost_artwork": {}, "auction_artwork": {}},
            "rows": {"match_score": []},
            "hashes": {
                "match_score": {},
                "lost_artwork": {},
                "auction_artwork": {},
            },
        }
        catalog = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            sync, "_build_data_content", return_value=content
        ), mock.patch.object(
            sync.gzip,
            "compress",
            return_value=b"x"
            * (sync.MAX_COMPRESSED_PAGE_BYTES - sync._PAGE_ENVELOPE_RESERVE_BYTES + 1),
        ):
            root = Path(temp_dir)
            with self.assertRaises(sync.UnsendableClosureError):
                sync._spool_data_content(
                    mock.Mock(),
                    root,
                    [],
                    {"10000000-0000-4000-8000-000000000001"},
                    set(),
                    [],
                    catalog=catalog,
                    budget=sync.WorkspaceBudget(root),
                    expected_matches=set(),
                    expected_lost={"10000000-0000-4000-8000-000000000001"},
                    expected_auction=set(),
                )
        catalog.verify_content.assert_called_once()
