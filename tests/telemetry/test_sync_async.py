"""Focused selective synchronization tests."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from telemetry import sync_codec
from telemetry import sync_http as sync


class _Settings:
    endpoint = "https://receiver.example/api/telemetry/sync/v3/pages"
    timeout_seconds = 10.0
    auth_token = "unit-test-static-bearer-token"


class AsyncApplyTests(unittest.TestCase):
    def test_final_data_page_opts_into_asynchronous_apply(self) -> None:
        encoded = sync_codec.encode_sync_page({"schema_version": 3})
        acknowledgement = {
            "sync_id": "sync-id",
            "phase": "data",
            "page_number": 0,
            "payload_sha256": encoded.uncompressed_sha256,
            "complete": False,
            "accepted": True,
            "status": "applying",
            "poll_after_seconds": 60,
        }
        response = mock.MagicMock()
        response.__enter__.return_value.status = 202
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
                phase="data",
                page_number=0,
                page_count=1,
            )

        self.assertEqual(result, acknowledgement)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Prefer"), "respond-async")

    def test_synchronous_finalization_remains_available_for_v3_compatibility(
        self,
    ) -> None:
        encoded = sync_codec.encode_sync_page({"schema_version": 3})
        acknowledgement = {
            "sync_id": "sync-id",
            "phase": "data",
            "page_number": 0,
            "payload_sha256": encoded.uncompressed_sha256,
            "complete": True,
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
                phase="data",
                page_number=0,
                page_count=1,
                prefer_async=False,
            )

        self.assertTrue(result["complete"])
        request = opener.open.call_args.args[0]
        self.assertIsNone(request.get_header("Prefer"))

    def test_status_poll_uses_short_timeout_and_same_receiver_origin(self) -> None:
        result = {
            "sync_id": "sync-id",
            "status": "complete",
            "complete": True,
            "failed": False,
            "operation_sha256": "a" * 64,
            "completed_at": "2026-08-18T12:00:00+00:00",
        }
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = json.dumps(result).encode()
        opener = mock.MagicMock()
        opener.open.return_value = response

        with mock.patch.object(sync, "build_opener", return_value=opener):
            actual = sync._get_operation_status(
                _Settings(),
                sync_id="sync-id",
                operation_sha256="a" * 64,
            )

        self.assertEqual(actual, result)
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://receiver.example/api/telemetry/sync/v3/operations/sync-id",
        )
        self.assertEqual(
            opener.open.call_args.kwargs["timeout"],
            min(_Settings.timeout_seconds, sync.ASYNC_POLL_REQUEST_TIMEOUT_SECONDS),
        )

    def test_async_apply_polls_every_minute_and_completes(self) -> None:
        pending = {
            "sync_id": "sync-id",
            "status": "applying",
            "complete": False,
            "failed": False,
            "operation_sha256": None,
        }
        complete = {
            "sync_id": "sync-id",
            "status": "complete",
            "complete": True,
            "failed": False,
            "operation_sha256": "a" * 64,
        }
        clock = [0.0]
        with mock.patch.object(
            sync, "_get_operation_status", side_effect=[pending, complete]
        ) as get_status, mock.patch.object(
            sync.time, "monotonic", side_effect=lambda: clock[0]
        ), mock.patch.object(
            sync.time,
            "sleep",
            side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ) as sleep:
            result = sync._wait_for_async_apply(
                _Settings(), sync_id="sync-id", operation_sha256="a" * 64
            )

        self.assertEqual(result, complete)
        self.assertEqual(get_status.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertAlmostEqual(sleep.call_args.args[0], 60, delta=0.1)

    def test_async_apply_is_failed_after_ten_minutes(self) -> None:
        pending = {
            "sync_id": "sync-id",
            "status": "applying",
            "complete": False,
            "failed": False,
            "operation_sha256": None,
        }
        clock = [0.0]
        with mock.patch.object(
            sync, "_get_operation_status", return_value=pending
        ) as get_status, mock.patch.object(
            sync.time, "monotonic", side_effect=lambda: clock[0]
        ), mock.patch.object(
            sync.time,
            "sleep",
            side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ) as sleep:
            with self.assertRaises(sync.AsyncApplyError):
                sync._wait_for_async_apply(
                    _Settings(), sync_id="sync-id", operation_sha256="a" * 64
                )

        self.assertEqual(get_status.call_count, 11)
        self.assertEqual(sleep.call_count, 10)
        for call in sleep.call_args_list:
            self.assertAlmostEqual(call.args[0], 60, delta=0.1)
        self.assertEqual(clock[0], 600)

    def test_completion_during_final_minute_is_observed_at_deadline(self) -> None:
        pending = {
            "sync_id": "sync-id",
            "status": "applying",
            "complete": False,
            "failed": False,
            "operation_sha256": None,
        }
        complete = {
            "sync_id": "sync-id",
            "status": "complete",
            "complete": True,
            "failed": False,
            "operation_sha256": "a" * 64,
        }
        clock = [0.0]
        with mock.patch.object(
            sync,
            "_get_operation_status",
            side_effect=[pending] * 10 + [complete],
        ) as get_status, mock.patch.object(
            sync.time, "monotonic", side_effect=lambda: clock[0]
        ), mock.patch.object(
            sync.time,
            "sleep",
            side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ):
            result = sync._wait_for_async_apply(
                _Settings(), sync_id="sync-id", operation_sha256="a" * 64
            )

        self.assertEqual(result, complete)
        self.assertEqual(get_status.call_count, 11)
        self.assertEqual(clock[0], 600)

    def test_receiver_failed_state_is_terminal(self) -> None:
        failed = {
            "sync_id": "sync-id",
            "status": "failed",
            "complete": False,
            "failed": True,
            "operation_sha256": None,
        }
        with mock.patch.object(
            sync, "_get_operation_status", return_value=failed
        ), mock.patch.object(sync.time, "sleep"):
            with self.assertRaises(sync.AsyncApplyError):
                sync._wait_for_async_apply(
                    _Settings(), sync_id="sync-id", operation_sha256="a" * 64
                )
