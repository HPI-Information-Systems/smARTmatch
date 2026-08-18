"""Telemetry daemon scheduling and child-process lifecycle."""

import os
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from threading import Event
from typing import Any

from matching_pipeline.shared.env import env_repo_root
from shared.service_health import HealthReporter
from telemetry.config import _telemetry_enabled
from telemetry.constants import (
    DAEMON_POLL_SECONDS,
    DEFAULT_PROCESS_DEADLINE_SECONDS,
    STARTUP_MAX_RETRY_SECONDS,
    STARTUP_MAX_TRANSIENT_FAILURES,
    TELEMETRY_MODULE,
    WORKER_DEADLINE_CLEANUP_GRACE_SECONDS,
    WORKER_EXIT_DEADLINE,
    WORKER_EXIT_NOOP,
    WORKER_EXIT_TERMINAL,
    WORKER_LAUNCH_RETRY_SECONDS,
    logger,
)
from telemetry.serialization import _as_utc
from telemetry.sync_workspace import cleanup_stale_sync_spools


def run_telemetry_daemon(
    stop_event: Event,
    *,
    now_fn=None,
    launch_worker=None,
    health: HealthReporter | None = None,
) -> int:
    """Schedule startup and UTC-daily attempts with bounded retry behavior."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    launch_worker = launch_worker or _launch_worker
    startup_pending = True
    startup_transient_failures = 0
    last_daily_started: date | None = None
    worker: subprocess.Popen[bytes] | None = None
    worker_trigger: str | None = None
    worker_started_at: float | None = None
    last_enabled: bool | None = None

    cleanup_stale_sync_spools()
    if health is not None:
        health.update("starting", "telemetry daemon initialization")
    logger.info(
        "Telemetry daemon started poll_seconds=%.0f startup_max_attempts=%d",
        DAEMON_POLL_SECONDS,
        STARTUP_MAX_TRANSIENT_FAILURES,
    )
    while not stop_event.is_set():
        enabled = _telemetry_enabled()
        if enabled != last_enabled:
            logger.info("Telemetry daemon configuration enabled=%s", enabled)
            last_enabled = enabled
            if health is not None:
                if enabled:
                    health.update("running", "telemetry is enabled; startup pending")
                else:
                    health.update("disabled", "telemetry is intentionally disabled")

        forced_deadline = False
        if (
            worker is not None
            and worker.poll() is None
            and worker_started_at is not None
            and time.monotonic() - worker_started_at
            > DEFAULT_PROCESS_DEADLINE_SECONDS + WORKER_DEADLINE_CLEANUP_GRACE_SECONDS
        ):
            logger.error(
                "Telemetry worker trigger=%s exceeded parent deadline; terminating",
                worker_trigger,
            )
            _stop_worker(worker)
            forced_deadline = True

        if worker is not None and (forced_deadline or worker.poll() is not None):
            return_code = WORKER_EXIT_DEADLINE if forced_deadline else worker.wait()
            completed_trigger = worker_trigger
            worker = None
            worker_trigger = None
            worker_started_at = None
            cleanup_stale_sync_spools()
            if return_code == 0:
                logger.info(
                    "Telemetry worker trigger=%s finished exit_code=%d",
                    completed_trigger,
                    return_code,
                )
                if health is not None:
                    health.update(
                        "healthy",
                        f"telemetry {completed_trigger} work succeeded",
                        trigger=completed_trigger,
                        exit_code=return_code,
                        startup_transient_failures=startup_transient_failures,
                    )
            elif return_code == WORKER_EXIT_NOOP:
                logger.info(
                    "Telemetry worker trigger=%s skipped already-completed attempt",
                    completed_trigger,
                )
                if health is not None:
                    health.heartbeat(trigger=completed_trigger, exit_code=return_code)
            else:
                logger.error(
                    "Telemetry worker trigger=%s failed exit_code=%d",
                    completed_trigger,
                    return_code,
                )

            if completed_trigger == "startup":
                if return_code == 0:
                    startup_pending = False
                    startup_transient_failures = 0
                elif return_code == WORKER_EXIT_TERMINAL:
                    startup_pending = False
                    logger.error(
                        "Startup telemetry failed terminally; no startup retry will run"
                    )
                    if health is not None:
                        health.update(
                            "unhealthy",
                            "startup telemetry failed terminally",
                            trigger="startup",
                            exit_code=return_code,
                        )
                else:
                    startup_transient_failures += 1
                    if startup_transient_failures >= STARTUP_MAX_TRANSIENT_FAILURES:
                        startup_pending = False
                        logger.error(
                            "Startup telemetry retry budget exhausted failures=%d",
                            startup_transient_failures,
                        )
                        if health is not None:
                            health.update(
                                "unhealthy",
                                "startup telemetry retry budget exhausted",
                                trigger="startup",
                                exit_code=return_code,
                                startup_transient_failures=startup_transient_failures,
                            )
                    else:
                        retry_seconds = min(
                            WORKER_LAUNCH_RETRY_SECONDS
                            * (2 ** (startup_transient_failures - 1)),
                            STARTUP_MAX_RETRY_SECONDS,
                        )
                        logger.warning(
                            "Startup telemetry failed transiently; retrying in "
                            "%.0f seconds attempt=%d/%d",
                            retry_seconds,
                            startup_transient_failures + 1,
                            STARTUP_MAX_TRANSIENT_FAILURES,
                        )
                        if health is not None:
                            health.update(
                                "degraded",
                                "startup telemetry failed transiently; retry pending",
                                trigger="startup",
                                exit_code=return_code,
                                startup_transient_failures=startup_transient_failures,
                            )
                        _wait_with_health(
                            stop_event,
                            retry_seconds,
                            health,
                            trigger="startup",
                            startup_transient_failures=startup_transient_failures,
                        )
                        continue
            elif return_code not in {0, WORKER_EXIT_NOOP} and health is not None:
                health.update(
                    "unhealthy",
                    f"daily telemetry failed with exit code {return_code}",
                    trigger="daily",
                    exit_code=return_code,
                )

        if worker is None and enabled:
            trigger: str | None = None
            current_date = _as_utc(now_fn()).date()
            if startup_pending:
                trigger = "startup"
            elif current_date != last_daily_started:
                trigger = "daily"

            if trigger is not None:
                cleanup_stale_sync_spools()
                logger.info(
                    "Telemetry worker launch started trigger=%s date=%s",
                    trigger,
                    current_date,
                )
                worker = launch_worker(trigger)
                if worker is None:
                    logger.error("Telemetry worker launch failed trigger=%s", trigger)
                    if trigger == "startup":
                        startup_transient_failures += 1
                        if startup_transient_failures >= STARTUP_MAX_TRANSIENT_FAILURES:
                            startup_pending = False
                            logger.error(
                                "Startup telemetry launch retry budget exhausted "
                                "failures=%d",
                                startup_transient_failures,
                            )
                            if health is not None:
                                health.update(
                                    "unhealthy",
                                    "startup telemetry worker could not be launched",
                                    trigger="startup",
                                    startup_transient_failures=startup_transient_failures,
                                )
                        else:
                            retry_seconds = min(
                                WORKER_LAUNCH_RETRY_SECONDS
                                * (2 ** (startup_transient_failures - 1)),
                                STARTUP_MAX_RETRY_SECONDS,
                            )
                            if health is not None:
                                health.update(
                                    "degraded",
                                    "startup telemetry worker launch retry pending",
                                    trigger="startup",
                                    startup_transient_failures=startup_transient_failures,
                                )
                            _wait_with_health(
                                stop_event,
                                retry_seconds,
                                health,
                                trigger="startup",
                                startup_transient_failures=startup_transient_failures,
                            )
                            continue
                    else:
                        last_daily_started = current_date
                        if health is not None:
                            health.update(
                                "unhealthy",
                                "daily telemetry worker could not be launched",
                                trigger="daily",
                            )
                else:
                    worker_trigger = trigger
                    worker_started_at = time.monotonic()
                    logger.info(
                        "Telemetry worker running trigger=%s pid=%s",
                        trigger,
                        getattr(worker, "pid", "unknown"),
                    )
                    if health is not None:
                        if health.state == "unhealthy":
                            health.heartbeat(trigger=trigger)
                        else:
                            health.update(
                                "running",
                                f"telemetry {trigger} work is running",
                                trigger=trigger,
                                worker_pid=getattr(worker, "pid", None),
                            )
                    if trigger == "daily":
                        last_daily_started = current_date

        if health is not None:
            health.heartbeat(trigger=worker_trigger)
        stop_event.wait(DAEMON_POLL_SECONDS)

    if health is not None:
        health.update("stopping", "telemetry daemon stopping", trigger=worker_trigger)
    logger.info("Telemetry daemon stopping active_trigger=%s", worker_trigger)
    if worker is not None:
        _stop_worker(worker)
    cleanup_stale_sync_spools()
    logger.info("Telemetry daemon stopped")
    return 0


def _wait_with_health(
    stop_event: Event,
    seconds: float,
    health: HealthReporter | None,
    **fields: Any,
) -> None:
    if health is None:
        stop_event.wait(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if stop_event.wait(min(30.0, remaining)):
            return
        health.heartbeat(**fields)


def _launch_worker(trigger: str) -> subprocess.Popen[bytes] | None:
    try:
        return subprocess.Popen(
            (sys.executable, "-m", TELEMETRY_MODULE, "--trigger", trigger),
            cwd=env_repo_root(),
            env=os.environ.copy(),
            start_new_session=True,
        )
    except OSError:
        logger.exception("Could not start %s telemetry worker", trigger)
        return None


def _stop_worker(worker: subprocess.Popen[bytes]) -> None:
    if worker.poll() is not None:
        worker.wait()
        return
    try:
        os.killpg(worker.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        worker.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        worker.wait()
