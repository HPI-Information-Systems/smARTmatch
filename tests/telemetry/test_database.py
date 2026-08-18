"""Focused offline telemetry tests."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from telemetry import database as telemetry


class MigrationLedgerTests(unittest.TestCase):
    def test_latest_applied_migration_is_absent_without_a_ledger(self) -> None:
        cursor = mock.Mock()
        cursor.fetchone.return_value = (None,)

        self.assertIsNone(telemetry._latest_applied_migration(cursor))
        cursor.execute.assert_called_once_with(
            "SELECT to_regclass('public.schema_migrations')"
        )

    def test_latest_applied_migration_includes_ledger_identity(self) -> None:
        applied_at = datetime(2026, 8, 16, 12, 30, tzinfo=timezone.utc)
        cursor = mock.Mock()
        cursor.fetchone.side_effect = [
            ("public.schema_migrations",),
            (22, "22_expand_telemetry_payload_bytes.sql", "a" * 64, applied_at),
        ]

        migration = telemetry._latest_applied_migration(cursor)

        self.assertEqual(
            migration,
            {
                "application_order": 22,
                "migration_name": "22_expand_telemetry_payload_bytes.sql",
                "checksum_sha256": "a" * 64,
                "applied_at": "2026-08-16T12:30:00Z",
            },
        )
        self.assertEqual(
            cursor.execute.call_args_list[-1].args[0],
            telemetry._LATEST_APPLIED_MIGRATION_SQL,
        )
