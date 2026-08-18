"""Focused selective synchronization tests."""

from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from telemetry import sync_delivery as sync
from telemetry.sync_errors import UnsendableClosureError
from telemetry.sync_models import RawPage
from telemetry.sync_utils import _canonical_json


class _Settings:
    endpoint = "https://receiver.example/api/telemetry/sync/v3/pages"
    timeout_seconds = 10.0
    auth_token = "unit-test-static-bearer-token"


class SyncDeliveryTests(unittest.TestCase):
    def test_inventory_response_selects_only_requested_data_for_final_phase(
        self,
    ) -> None:
        lost_id = "10000000-0000-4000-8000-000000000001"
        auction_id = "20000000-0000-4000-8000-000000000001"

        def spool(directory: Path, name: str):
            directory.mkdir(parents=True, exist_ok=True)
            content = (
                {
                    "inventory": {
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
                }
                if name == "inventory"
                else {
                    "entities": {"lost_artwork": {}, "auction_artwork": {}},
                    "rows": {"match_score": []},
                    "hashes": {
                        "match_score": {},
                        "lost_artwork": {},
                        "auction_artwork": {},
                    },
                }
            )
            raw = _canonical_json(content)
            path = directory / "0.json"
            path.write_bytes(raw)
            return [RawPage(path, hashlib.sha256(raw).hexdigest(), {})]

        inventory_connection = mock.MagicMock()
        data_connection = mock.MagicMock()
        requested_counts = None

        def spool_data(directory: Path, **kwargs):
            nonlocal requested_counts
            requested_counts = kwargs["catalog"].requested_counts()
            return spool(directory, "data")

        def summary_factory(conn):
            self.assertIs(conn, inventory_connection)
            return {}

        def post_page(*_args, **kwargs):
            if kwargs["phase"] == "inventory":
                self.assertTrue(inventory_connection.close.called)
                return {
                    "needed": {
                        "match_score": [{"lost_id": lost_id, "auction_id": auction_id}],
                        "lost_artwork": [],
                        "auction_artwork": [],
                    }
                }
            self.assertTrue(data_connection.close.called)
            return {}

        with mock.patch.object(
            sync,
            "_snapshot_connection",
            side_effect=[inventory_connection, data_connection],
        ) as snapshot, mock.patch.object(
            sync,
            "_spool_inventory_pages",
            side_effect=lambda directory, **_kwargs: spool(directory, "inventory"),
        ) as inventory_spool, mock.patch.object(
            sync,
            "_spool_data_pages",
            side_effect=spool_data,
        ) as data_spool, mock.patch.object(
            sync,
            "_post_page_with_retries",
            side_effect=post_page,
        ) as post, mock.patch.object(
            sync, "uuid4", return_value="30000000-0000-4000-8000-000000000001"
        ), mock.patch.object(
            sync, "_sleep_before_next_page"
        ) as pacing:
            with self.assertLogs(sync.logger, level="INFO") as captured_logs:
                result = sync.deliver_sync_operation(
                    _Settings(),
                    trigger="daily",
                    generated_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
                    summary_factory=summary_factory,
                )

        self.assertEqual(result.page_count, 2)
        messages = "\n".join(captured_logs.output)
        self.assertIn("phase=inventory page=1/1 status=sending", messages)
        self.assertIn("phase=inventory page=1/1 status=acknowledged", messages)
        self.assertIn("phase=data page=1/1 status=sending", messages)
        self.assertIn("phase=data page=1/1 status=acknowledged", messages)
        self.assertIn("Telemetry sync complete", messages)
        self.assertEqual(
            [call.kwargs["phase"] for call in post.call_args_list],
            ["inventory", "data"],
        )
        self.assertEqual(requested_counts, (1, 1, 1))
        self.assertIs(inventory_spool.call_args.kwargs["conn"], inventory_connection)
        self.assertIs(data_spool.call_args.kwargs["conn"], data_connection)
        inventory_connection.close.assert_called_once_with()
        data_connection.close.assert_called_once_with()
        self.assertEqual(snapshot.call_count, 2)
        pacing.assert_called_once()

    def test_unsendable_data_fails_before_any_data_post(self) -> None:
        def inventory_spool(directory: Path, **_kwargs):
            directory.mkdir(parents=True, exist_ok=True)
            content = {
                "inventory": {
                    "match_score": {},
                    "lost_artwork": {},
                    "auction_artwork": {},
                }
            }
            raw = _canonical_json(content)
            path = directory / "0.json"
            path.write_bytes(raw)
            return [RawPage(path, hashlib.sha256(raw).hexdigest(), {})]

        with mock.patch.object(
            sync,
            "_snapshot_connection",
            side_effect=[mock.MagicMock(), mock.MagicMock()],
        ), mock.patch.object(
            sync, "_spool_inventory_pages", side_effect=inventory_spool
        ), mock.patch.object(
            sync,
            "_spool_data_pages",
            side_effect=UnsendableClosureError("closure too large"),
        ), mock.patch.object(
            sync, "_post_page_with_retries", return_value={"needed": {}}
        ) as post:
            with self.assertRaises(UnsendableClosureError):
                sync.deliver_sync_operation(
                    _Settings(),
                    trigger="startup",
                    generated_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
                    summary={},
                )

        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["phase"], "inventory")
