"""Startup and once-per-day telemetry delivery orchestration."""

from datetime import datetime, timezone

from telemetry.config import load_telemetry_settings
from telemetry.constants import logger
from telemetry.database import (
    _claim_daily_attempt,
    _record_daily_result,
    _record_daily_sync_result,
)
from telemetry.models import TelemetryDeadlineExceeded
from telemetry.serialization import _as_utc, _isoformat
from telemetry.summary import _collect_non_database_telemetry, collect_telemetry_payload
from telemetry.sync_delivery import deliver_sync_operation
from telemetry.sync_errors import is_terminal_sync_failure


def try_send_daily_telemetry(now: datetime | None = None) -> str:
    """Attempt at most one telemetry delivery per UTC day, without raising."""
    try:
        settings = load_telemetry_settings()
    except TelemetryDeadlineExceeded:
        raise
    except Exception:
        logger.exception("Telemetry configuration is invalid; skipping daily attempt")
        return "invalid_configuration"
    if settings is None:
        logger.info("Daily telemetry attempt skipped: telemetry is disabled")
        return "disabled"

    current = _as_utc(now or datetime.now(timezone.utc))
    attempt_date = current.date()
    logger.info("Daily telemetry attempt started attempt_date=%s", attempt_date)
    try:
        if not _claim_daily_attempt(attempt_date):
            logger.info(
                "Daily telemetry attempt skipped attempt_date=%s status=already_attempted",
                attempt_date,
            )
            return "already_attempted"
    except TelemetryDeadlineExceeded:
        raise
    except Exception:
        logger.exception(
            "Could not claim the daily telemetry attempt attempt_date=%s",
            attempt_date,
        )
        return "claim_failed"

    try:
        non_database_snapshot = _collect_non_database_telemetry(settings)

        def build_summary(conn):
            summary = collect_telemetry_payload(
                settings,
                generated_at=current,
                trigger="daily",
                conn=conn,
                non_database_snapshot=non_database_snapshot,
            )
            logger.info(
                "Daily telemetry summary ready attempt_date=%s",
                attempt_date,
            )
            return summary

        result = deliver_sync_operation(
            settings,
            trigger="daily",
            generated_at=current,
            summary_factory=build_summary,
        )
    except TelemetryDeadlineExceeded as exc:
        if not _record_daily_result(
            attempt_date,
            status="failed",
            http_status=None,
            error_class=type(exc).__name__,
        ):
            logger.error(
                "Telemetry deadline result requires reconciliation attempt_date=%s",
                attempt_date,
            )
        raise
    except Exception as exc:
        result_recorded = _record_daily_result(
            attempt_date,
            status="failed",
            http_status=getattr(exc, "status", None),
            error_class=type(exc).__name__,
        )
        terminal = is_terminal_sync_failure(exc)
        logger.exception(
            "Daily telemetry sync failed attempt_date=%s terminal=%s",
            attempt_date,
            terminal,
        )
        if not result_recorded:
            return "reconciliation_required"
        return "terminal_failure" if terminal else "transient_failure"

    result_recorded = _record_daily_sync_result(attempt_date, result)
    if not result_recorded:
        logger.error(
            "Telemetry was delivered, but its daily result could not be persisted"
        )
        return "reconciliation_required"
    logger.info(
        "Daily telemetry sync sent: sync_id=%s pages=%d compressed_bytes=%d",
        result.sync_id,
        result.page_count,
        result.total_compressed_bytes,
    )
    return "sent"


def try_send_startup_telemetry(now: datetime | None = None) -> str:
    """Attempt one delivery for this scheduler/container start, without raising."""
    try:
        settings = load_telemetry_settings()
    except TelemetryDeadlineExceeded:
        raise
    except Exception:
        logger.exception("Telemetry configuration is invalid; skipping startup attempt")
        return "invalid_configuration"
    if settings is None:
        logger.info("Startup telemetry attempt skipped: telemetry is disabled")
        return "disabled"

    current = _as_utc(now or datetime.now(timezone.utc))
    logger.info(
        "Startup telemetry attempt started generated_at=%s", _isoformat(current)
    )
    try:
        non_database_snapshot = _collect_non_database_telemetry(settings)

        def build_summary(conn):
            summary = collect_telemetry_payload(
                settings,
                generated_at=current,
                trigger="startup",
                conn=conn,
                non_database_snapshot=non_database_snapshot,
            )
            logger.info("Startup telemetry summary ready")
            return summary

        result = deliver_sync_operation(
            settings,
            trigger="startup",
            generated_at=current,
            summary_factory=build_summary,
        )
    except TelemetryDeadlineExceeded:
        raise
    except Exception as exc:
        terminal = is_terminal_sync_failure(exc)
        logger.exception("Startup telemetry sync failed terminal=%s", terminal)
        return "terminal_failure" if terminal else "transient_failure"

    logger.info(
        "Startup telemetry sync sent: sync_id=%s pages=%d compressed_bytes=%d",
        result.sync_id,
        result.page_count,
        result.total_compressed_bytes,
    )
    return "sent"
