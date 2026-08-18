"""Focused offline telemetry tests."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from telemetry import database
from telemetry import delivery as telemetry
from telemetry.models import TelemetrySettings
from telemetry.sync_models import SyncDeliveryResult


class DailyDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        non_database = mock.patch.object(
            telemetry,
            "_collect_non_database_telemetry",
            return_value=mock.Mock(),
        )
        non_database.start()
        self.addCleanup(non_database.stop)
        self.settings = TelemetrySettings(
            endpoint="https://example.test/collect",
            auth_token="unit-test-static-bearer-token",
            image_root=Path("/images"),
            timeout_seconds=3,
            match_expiration_seconds=30 * 86400,
        )
        self.now = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
        self.sync_result = SyncDeliveryResult(
            sync_id="10000000-0000-4000-8000-000000000001",
            page_count=3,
            total_compressed_bytes=1234,
            operation_sha256="b" * 64,
            last_page=mock.Mock(),
        )

    @mock.patch.object(telemetry, "_record_daily_sync_result", return_value=True)
    @mock.patch.object(telemetry, "deliver_sync_operation")
    @mock.patch.object(telemetry, "collect_telemetry_payload", return_value={})
    @mock.patch.object(telemetry, "_claim_daily_attempt", return_value=True)
    @mock.patch.object(telemetry, "load_telemetry_settings")
    def test_successful_attempt_is_one_claimed_paginated_sync(
        self,
        settings,
        claim,
        collect,
        deliver,
        record,
    ) -> None:
        settings.return_value = self.settings
        inventory_conn = mock.Mock()

        def run_delivery(*_args, **kwargs):
            self.assertEqual(kwargs["summary_factory"](inventory_conn), {})
            return self.sync_result

        deliver.side_effect = run_delivery
        self.assertEqual(telemetry.try_send_daily_telemetry(self.now), "sent")
        claim.assert_called_once_with(date(2026, 8, 14))
        collect.assert_called_once_with(
            self.settings,
            generated_at=self.now,
            trigger="daily",
            conn=inventory_conn,
            non_database_snapshot=mock.ANY,
        )
        deliver.assert_called_once_with(
            self.settings,
            trigger="daily",
            generated_at=self.now,
            summary_factory=mock.ANY,
        )
        record.assert_called_once_with(date(2026, 8, 14), self.sync_result)

    @mock.patch.object(telemetry, "_record_daily_sync_result", return_value=False)
    @mock.patch.object(telemetry, "deliver_sync_operation")
    @mock.patch.object(telemetry, "_claim_daily_attempt", return_value=True)
    @mock.patch.object(telemetry, "load_telemetry_settings")
    def test_delivery_is_not_successful_when_result_persistence_fails(
        self,
        settings,
        _claim,
        deliver,
        record,
    ) -> None:
        settings.return_value = self.settings
        deliver.return_value = self.sync_result
        self.assertEqual(
            telemetry.try_send_daily_telemetry(self.now),
            "reconciliation_required",
        )
        record.assert_called_once_with(date(2026, 8, 14), self.sync_result)

    @mock.patch.object(telemetry, "_record_daily_result")
    @mock.patch.object(telemetry, "deliver_sync_operation", side_effect=TimeoutError)
    @mock.patch.object(telemetry, "collect_telemetry_payload", return_value={})
    @mock.patch.object(telemetry, "_claim_daily_attempt", return_value=True)
    @mock.patch.object(telemetry, "load_telemetry_settings")
    def test_failed_sync_is_not_raised_to_the_pipeline(
        self,
        settings,
        _claim,
        _collect,
        _deliver,
        record,
    ) -> None:
        settings.return_value = self.settings
        self.assertEqual(
            telemetry.try_send_daily_telemetry(self.now), "transient_failure"
        )
        self.assertEqual(record.call_args.kwargs["status"], "failed")
        self.assertEqual(record.call_args.kwargs["error_class"], "TimeoutError")

    @mock.patch.object(telemetry, "_record_daily_result", return_value=False)
    @mock.patch.object(telemetry, "deliver_sync_operation", side_effect=TimeoutError)
    @mock.patch.object(telemetry, "_claim_daily_attempt", return_value=True)
    @mock.patch.object(telemetry, "load_telemetry_settings")
    def test_failed_delivery_requires_reconciliation_when_failure_is_not_persisted(
        self,
        settings,
        _claim,
        _deliver,
        record,
    ) -> None:
        settings.return_value = self.settings
        self.assertEqual(
            telemetry.try_send_daily_telemetry(self.now),
            "reconciliation_required",
        )
        record.assert_called_once()

    @mock.patch.object(telemetry, "deliver_sync_operation")
    @mock.patch.object(telemetry, "collect_telemetry_payload", return_value={})
    @mock.patch.object(telemetry, "_claim_daily_attempt")
    @mock.patch.object(telemetry, "load_telemetry_settings")
    def test_startup_trigger_creates_one_independent_sync(
        self,
        settings,
        claim,
        collect,
        deliver,
    ) -> None:
        settings.return_value = self.settings
        inventory_conn = mock.Mock()

        def run_delivery(*_args, **kwargs):
            self.assertEqual(kwargs["summary_factory"](inventory_conn), {})
            return self.sync_result

        deliver.side_effect = run_delivery
        self.assertEqual(telemetry.try_send_startup_telemetry(self.now), "sent")
        claim.assert_not_called()
        collect.assert_called_once_with(
            self.settings,
            generated_at=self.now,
            trigger="startup",
            conn=inventory_conn,
            non_database_snapshot=mock.ANY,
        )
        deliver.assert_called_once_with(
            self.settings,
            trigger="startup",
            generated_at=self.now,
            summary_factory=mock.ANY,
        )

    @mock.patch.object(telemetry, "load_telemetry_settings")
    @mock.patch.object(telemetry, "_claim_daily_attempt", return_value=False)
    def test_existing_daily_claim_skips_collection(self, claim, settings) -> None:
        settings.return_value = self.settings
        with mock.patch.object(telemetry, "collect_telemetry_payload") as collect:
            self.assertEqual(
                telemetry.try_send_daily_telemetry(self.now), "already_attempted"
            )
        claim.assert_called_once()
        collect.assert_not_called()

    def test_sync_result_update_requires_an_existing_daily_row(self) -> None:
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        with mock.patch.object(
            database, "connect", return_value=connection
        ), self.assertLogs(telemetry.logger, level="ERROR"):
            self.assertFalse(
                database._record_daily_sync_result(date(2026, 8, 14), self.sync_result)
            )
        connection.rollback.assert_called_once_with()
