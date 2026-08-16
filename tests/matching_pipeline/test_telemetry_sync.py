"""Protocol tests for selective, paginated telemetry synchronization."""

from __future__ import annotations

import gzip
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from matching_pipeline.shared import telemetry_sync as sync


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
            sync, "_build_data_content", return_value=graph_hashes
        ):
            fake_conn = connection.return_value
            pages = sync._spool_inventory_pages(directory)
            fake_conn.rollback.assert_called_once()
            payload = json.loads(pages[0].path.read_text())
            self.assertEqual(len(payload["inventory"]["match_score"]), 2)
            self.assertEqual(payload["inventory"]["lost_artwork"].keys(), {"lost-1"})
            self.assertEqual(len(payload["inventory"]["auction_artwork"]), 2)


class SyncDeliveryTests(unittest.TestCase):
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
            raw = sync._canonical_json(content)
            path = directory / "0.json"
            path.write_bytes(raw)
            return [sync.RawPage(path, sync.hashlib.sha256(raw).hexdigest(), {})]

        with mock.patch.object(
            sync, "_snapshot_connection"
        ) as snapshot, mock.patch.object(
            sync,
            "_spool_inventory_pages",
            side_effect=lambda directory, **_kwargs: spool(directory, "inventory"),
        ) as inventory_spool, mock.patch.object(
            sync,
            "_spool_data_pages",
            side_effect=lambda directory, **_kwargs: spool(directory, "data"),
        ) as data_spool, mock.patch.object(
            sync,
            "_post_page_with_retries",
            side_effect=[
                {
                    "needed": {
                        "match_score": [{"lost_id": lost_id, "auction_id": auction_id}],
                        "lost_artwork": [],
                        "auction_artwork": [],
                    }
                },
                {},
            ],
        ) as post, mock.patch.object(
            sync, "uuid4", return_value="30000000-0000-4000-8000-000000000001"
        ):
            with self.assertLogs(sync.logger, level="INFO") as captured_logs:
                result = sync.deliver_sync_operation(
                    _Settings(),
                    trigger="daily",
                    generated_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
                    summary={},
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
        self.assertEqual(
            data_spool.call_args.kwargs["requested_matches"],
            {(lost_id, auction_id)},
        )
        self.assertEqual(data_spool.call_args.kwargs["requested_lost"], {lost_id})
        self.assertEqual(data_spool.call_args.kwargs["requested_auction"], {auction_id})
        self.assertIs(inventory_spool.call_args.kwargs["conn"], snapshot.return_value)
        self.assertIs(data_spool.call_args.kwargs["conn"], snapshot.return_value)

    def test_terminal_page_failure_logs_each_retry_and_final_failure(self) -> None:
        encoded = sync.encode_sync_page({"schema_version": 3})
        with mock.patch.object(
            sync,
            "_post_page",
            side_effect=TimeoutError("receiver unavailable"),
        ) as post, mock.patch.object(sync.time, "sleep") as sleep, self.assertLogs(
            sync.logger, level="WARNING"
        ) as captured_logs:
            with self.assertRaises(TimeoutError):
                sync._post_page_with_retries(
                    _Settings(),
                    encoded,
                    sync_id="sync-id",
                    phase="data",
                    page_number=1,
                    page_count=3,
                )

        self.assertEqual(post.call_count, sync.PAGE_RETRIES)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        messages = "\n".join(captured_logs.output)
        self.assertIn("phase=data page=2/3 attempt=1/3 retry_seconds=1", messages)
        self.assertIn("phase=data page=2/3 attempt=2/3 retry_seconds=2", messages)
        self.assertIn("phase=data page=2/3 attempt=3/3", messages)
        self.assertIn("TimeoutError: receiver unavailable", messages)

    def test_receiver_acknowledgement_must_match_phase_and_page(self) -> None:
        encoded = sync.encode_sync_page({"schema_version": 3})
        acknowledgement = {
            "sync_id": "sync-id",
            "phase": "inventory",
            "page_number": 0,
            "payload_sha256": encoded.uncompressed_sha256,
            "needed": {},
        }
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = json.dumps(
            acknowledgement
        ).encode()
        opener = mock.MagicMock()
        opener.open.return_value = response
        with mock.patch.object(sync, "build_opener", return_value=opener):
            result = sync._post_page(
                _Settings(),
                encoded,
                sync_id="sync-id",
                phase="inventory",
                page_number=0,
                page_count=1,
            )
        self.assertEqual(result, acknowledgement)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("X-smartmatch-phase"), "inventory")
        self.assertEqual(
            request.get_header("Authorization"),
            "Bearer unit-test-static-bearer-token",
        )


class tempfile_directory:
    def __enter__(self) -> Path:
        import tempfile

        self._temporary = tempfile.TemporaryDirectory()
        return Path(self._temporary.name)

    def __exit__(self, *_args) -> None:
        self._temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
