"""Focused selective synchronization tests."""

from __future__ import annotations

import json
import unittest
from http.client import IncompleteRead
from unittest import mock

from telemetry import sync_codec, sync_errors
from telemetry import sync_http as sync


class _Settings:
    endpoint = "https://receiver.example/api/telemetry/sync/v3/pages"
    timeout_seconds = 10.0
    auth_token = "unit-test-static-bearer-token"


class SyncHttpTests(unittest.TestCase):
    def test_successful_page_pacing_uses_configured_random_jitter(self) -> None:
        settings = _Settings()
        settings.page_delay_min_seconds = 0.25
        settings.page_delay_max_seconds = 0.5
        with mock.patch.object(
            sync.random, "uniform", return_value=0.375
        ) as uniform, mock.patch.object(sync.time, "sleep") as sleep:
            delay = sync._sleep_before_next_page(settings)

        self.assertEqual(delay, 0.375)
        uniform.assert_called_once_with(0.25, 0.5)
        sleep.assert_called_once_with(0.375)

    def test_page_pacing_defaults_to_disabled_for_protocol_test_settings(self) -> None:
        with mock.patch.object(sync.random, "uniform") as uniform, mock.patch.object(
            sync.time, "sleep"
        ) as sleep:
            delay = sync._sleep_before_next_page(_Settings())

        self.assertEqual(delay, 0.0)
        uniform.assert_not_called()
        sleep.assert_not_called()

    def test_terminal_page_failure_logs_each_retry_and_final_failure(self) -> None:
        encoded = sync_codec.encode_sync_page({"schema_version": 3})
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

    def test_retries_are_debited_from_wire_transfer_budget(self) -> None:
        encoded = sync_codec.encode_sync_page({"schema_version": 3})
        budget = sync.TransferBudget(max_bytes=len(encoded.body))
        with mock.patch.object(
            sync, "_post_page", side_effect=TimeoutError("timeout after send")
        ) as post, mock.patch.object(sync.time, "sleep"):
            with self.assertRaises(sync_errors.SyncWorkspaceLimitError):
                sync._post_page_with_retries(
                    _Settings(),
                    encoded,
                    sync_id="sync-id",
                    phase="data",
                    page_number=0,
                    page_count=1,
                    transfer_budget=budget,
                )
        post.assert_called_once()
        self.assertEqual(budget.attempted_bytes, len(encoded.body))

    def test_truncated_http_response_is_retried_as_transport_failure(self) -> None:
        encoded = sync_codec.encode_sync_page({"schema_version": 3})
        truncated_response = IncompleteRead(b'{"acknowledged":', 4)
        self.assertTrue(sync.is_transient_sync_failure(truncated_response))
        self.assertFalse(sync_errors.is_terminal_sync_failure(truncated_response))

        with mock.patch.object(
            sync,
            "_post_page",
            side_effect=[truncated_response, {"acknowledged": True}],
        ) as post, mock.patch.object(sync.time, "sleep") as sleep:
            result = sync._post_page_with_retries(
                _Settings(),
                encoded,
                sync_id="sync-id",
                phase="inventory",
                page_number=0,
                page_count=1,
            )

        self.assertEqual(result, {"acknowledged": True})
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_every_5xx_http_failure_is_retryable(self) -> None:
        for status in range(500, 600):
            with self.subTest(status=status):
                error = sync.SyncHttpError(status)
                self.assertTrue(error.retryable)
                self.assertTrue(sync.is_transient_sync_failure(error))
                self.assertFalse(sync_errors.is_terminal_sync_failure(error))

    def test_terminal_http_failure_is_not_retried(self) -> None:
        encoded = sync_codec.encode_sync_page({"schema_version": 3})
        with mock.patch.object(
            sync,
            "_post_page",
            side_effect=sync.SyncHttpError(400, "invalid request"),
        ) as post, mock.patch.object(sync.time, "sleep") as sleep:
            with self.assertRaises(sync.SyncHttpError):
                sync._post_page_with_retries(
                    _Settings(),
                    encoded,
                    sync_id="sync-id",
                    phase="inventory",
                    page_number=0,
                    page_count=1,
                )
        post.assert_called_once()
        sleep.assert_not_called()

    def test_unclassified_programming_failure_is_terminal(self) -> None:
        self.assertTrue(sync_errors.is_terminal_sync_failure(KeyError("bug")))
        self.assertFalse(sync.is_transient_sync_failure(KeyError("bug")))

    def test_retryable_http_failure_honors_bounded_retry_after(self) -> None:
        encoded = sync_codec.encode_sync_page({"schema_version": 3})
        with mock.patch.object(
            sync,
            "_post_page",
            side_effect=[
                sync.SyncHttpError(429, retry_after_seconds=120),
                {"acknowledged": True},
            ],
        ), mock.patch.object(sync.time, "sleep") as sleep:
            result = sync._post_page_with_retries(
                _Settings(),
                encoded,
                sync_id="sync-id",
                phase="inventory",
                page_number=0,
                page_count=1,
            )
        self.assertEqual(result, {"acknowledged": True})
        sleep.assert_called_once_with(sync.MAX_RETRY_AFTER_SECONDS)

    def test_receiver_acknowledgement_must_match_phase_and_page(self) -> None:
        encoded = sync_codec.encode_sync_page({"schema_version": 3})
        acknowledgement = {
            "sync_id": "sync-id",
            "phase": "inventory",
            "page_number": 0,
            "payload_sha256": encoded.uncompressed_sha256,
            "complete": False,
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

    def test_acknowledgement_rejects_bool_page_number_and_wrong_complete(self) -> None:
        encoded = sync_codec.encode_sync_page({"schema_version": 3})
        for page_number, complete in ((False, False), (0, True)):
            acknowledgement = {
                "sync_id": "sync-id",
                "phase": "inventory",
                "page_number": page_number,
                "payload_sha256": encoded.uncompressed_sha256,
                "complete": complete,
                "needed": {},
            }
            response = mock.MagicMock()
            response.__enter__.return_value.status = 200
            response.__enter__.return_value.read.return_value = json.dumps(
                acknowledgement
            ).encode()
            opener = mock.MagicMock()
            opener.open.return_value = response
            with self.subTest(
                page_number=page_number, complete=complete
            ), mock.patch.object(sync, "build_opener", return_value=opener):
                with self.assertRaises(sync.SyncProtocolError):
                    sync._post_page(
                        _Settings(),
                        encoded,
                        sync_id="sync-id",
                        phase="inventory",
                        page_number=0,
                        page_count=1,
                    )
