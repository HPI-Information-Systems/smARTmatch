"""Compatibility contracts for the established telemetry import paths."""

from telemetry import cli, delivery, models, sync_delivery, sync_models
from telemetry import telemetry as sender
from telemetry import telemetry_sync as sync


def test_sender_facade_keeps_cli_and_delivery_entry_points() -> None:
    assert sender.main is cli.main
    assert sender.try_send_daily_telemetry is delivery.try_send_daily_telemetry
    assert sender.try_send_startup_telemetry is delivery.try_send_startup_telemetry
    assert sender.TelemetrySettings is models.TelemetrySettings
    assert sender.TelemetryDeadlineExceeded is models.TelemetryDeadlineExceeded


def test_sync_facade_keeps_operation_contract_types() -> None:
    assert sync.deliver_sync_operation is sync_delivery.deliver_sync_operation
    assert sync.RawPage is sync_models.RawPage
    assert sync.EncodedSyncPage is sync_models.EncodedSyncPage
    assert sync.SyncDeliveryResult is sync_models.SyncDeliveryResult
