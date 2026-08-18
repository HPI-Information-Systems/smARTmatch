"""Bounded, opt-in daily telemetry for the matching pipeline."""

import argparse
import signal
from threading import Event

from shared.service_health import HealthReporter
from telemetry.constants import (
    DEFAULT_PROCESS_DEADLINE_SECONDS,
    WORKER_EXIT_DEADLINE,
    WORKER_EXIT_NOOP,
    WORKER_EXIT_TERMINAL,
    WORKER_EXIT_TRANSIENT,
    logger,
)
from telemetry.daemon import run_telemetry_daemon
from telemetry.delivery import try_send_daily_telemetry, try_send_startup_telemetry
from telemetry.models import TelemetryDeadlineExceeded


def _run_one_shot(trigger: str) -> int:
    def deadline_reached(_signum: int, _frame: object) -> None:
        raise TelemetryDeadlineExceeded(
            f"Telemetry worker exceeded {DEFAULT_PROCESS_DEADLINE_SECONDS} seconds"
        )

    logger.info(
        "Telemetry worker started trigger=%s deadline_seconds=%d",
        trigger,
        DEFAULT_PROCESS_DEADLINE_SECONDS,
    )
    alarm_available = hasattr(signal, "SIGALRM")
    previous_handler = None
    if alarm_available:
        previous_handler = signal.signal(signal.SIGALRM, deadline_reached)
        signal.alarm(DEFAULT_PROCESS_DEADLINE_SECONDS)
    try:
        try:
            if trigger == "startup":
                outcome = try_send_startup_telemetry()
            else:
                outcome = try_send_daily_telemetry()
        except TelemetryDeadlineExceeded:
            logger.exception(
                "Telemetry worker trigger=%s exceeded deadline_seconds=%d",
                trigger,
                DEFAULT_PROCESS_DEADLINE_SECONDS,
            )
            return WORKER_EXIT_DEADLINE
        logger.info("Telemetry worker finished trigger=%s outcome=%s", trigger, outcome)
    finally:
        if alarm_available:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
    if outcome == "sent":
        return 0
    if outcome == "already_attempted":
        return WORKER_EXIT_NOOP
    if outcome in {"invalid_configuration", "disabled", "terminal_failure"}:
        return WORKER_EXIT_TERMINAL
    return WORKER_EXIT_TRANSIENT


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run the independent startup/daily telemetry scheduler.",
    )
    parser.add_argument(
        "--trigger",
        choices=("startup", "daily"),
        default="daily",
        help="One-shot trigger used by daemon worker processes.",
    )
    return parser.parse_args()


def main() -> int:
    from shared.logging_adapter import configure_logging

    configure_logging()
    args = _parse_args()
    if not args.daemon:
        return _run_one_shot(args.trigger)

    health = HealthReporter.from_environment("telemetry")
    health.update("starting", "telemetry daemon initialization")
    stop_event = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        result = run_telemetry_daemon(stop_event, health=health)
    except BaseException as exc:
        health.update(
            "unhealthy",
            f"telemetry daemon failure: {type(exc).__name__}",
            error_class=type(exc).__name__,
        )
        raise
    health.update("stopping", "telemetry daemon stopped")
    return result
