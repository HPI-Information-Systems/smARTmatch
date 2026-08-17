#!/usr/bin/env python3
"""Run SmartMatch pipeline stages sequentially on a fixed minute cadence."""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Mapping

from shared.logging_adapter import configure_logging, get_logger
from shared.service_health import HealthReporter

DEFAULT_INTERVAL_MINUTES = 1.0
POLL_SECONDS = 0.5
SHUTDOWN_TIMEOUT_SECONDS = 30.0
SKIP_IMAGE_MATCHING_ENV = "SMARTMATCH_SKIP_IMAGE_MATCHING"
_PR_SET_CHILD_SUBREAPER = 36

_APP_ROOT = Path(__file__).resolve().parents[1]
_PROC_ROOT = Path("/proc")
logger = get_logger(__name__)


@dataclass(frozen=True)
class PipelineStep:
    key: str
    name: str
    command: tuple[str, ...]
    cwd: Path


@dataclass(frozen=True)
class ProcStat:
    process_id: int
    state: str
    parent_process_id: int
    process_group_id: int


PIPELINE_STEPS = (
    PipelineStep(
        "image-blocking",
        "image blocking",
        (sys.executable, "-m", "matching_pipeline.image_blocking", "--no-compile"),
        _APP_ROOT,
    ),
    PipelineStep(
        "image-matching",
        "image matching",
        (sys.executable, "-m", "matching_pipeline.image_matching"),
        _APP_ROOT,
    ),
    PipelineStep(
        "metadata-extraction",
        "metadata extraction and normalization",
        (sys.executable, "-m", "matching_pipeline.metadata_extraction"),
        _APP_ROOT,
    ),
    PipelineStep(
        "metadata-matching",
        "metadata matching",
        (sys.executable, "-m", "matching_pipeline.metadata_matching"),
        _APP_ROOT,
    ),
    PipelineStep(
        "image-cleanup",
        "unmatched auction image cleanup",
        (sys.executable, "-m", "matching_pipeline.image_cleanup", "--apply"),
        _APP_ROOT,
    ),
)


def main() -> int:
    configure_logging()
    health = HealthReporter.from_environment("matching_pipeline")
    health.update("starting", "scheduler initialization")
    try:
        args = _parse_args()
        _set_child_subreaper(True)
        stop_event = Event()
        _install_signal_handlers(stop_event)
        result = run_scheduler(
            args.interval_minutes * 60.0,
            stop_event,
            health=health,
        )
    except BaseException as exc:
        health.update(
            "unhealthy",
            f"scheduler startup/runtime failure: {type(exc).__name__}",
            error_class=type(exc).__name__,
        )
        raise
    health.update("stopping", "scheduler stopped")
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=DEFAULT_INTERVAL_MINUTES,
        help="Minutes between cycle starts (default: 1).",
    )
    args = parser.parse_args()
    if args.interval_minutes <= 0:
        parser.error("--interval-minutes must be greater than 0")
    return args


def _install_signal_handlers(stop_event: Event) -> None:
    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def run_scheduler(
    interval_seconds: float,
    stop_event: Event,
    *,
    health: HealthReporter | None = None,
) -> int:
    next_run_at = time.monotonic()
    cycle_number = 0
    consecutive_failed_cycles = 0
    _log(f"pipeline scheduler started: interval={interval_seconds / 60.0:g}m")
    if health is not None:
        health.update("running", "waiting for first pipeline cycle", cycle=0)

    while not stop_event.is_set():
        now = time.monotonic()
        if now < next_run_at:
            if health is not None:
                health.heartbeat(cycle=cycle_number)
            stop_event.wait(min(POLL_SECONDS, next_run_at - now))
            continue

        cycle_number += 1
        if health is None:
            failed_steps = _run_cycle(cycle_number, stop_event)
        else:
            failed_steps = _run_cycle(cycle_number, stop_event, health=health)
        if stop_event.is_set():
            break
        if failed_steps:
            consecutive_failed_cycles += 1
            if health is not None:
                health.update(
                    "unhealthy",
                    "pipeline cycle failed",
                    cycle=cycle_number,
                    failed_steps=failed_steps,
                    consecutive_failed_cycles=consecutive_failed_cycles,
                )
        else:
            consecutive_failed_cycles = 0
            if health is not None:
                health.update(
                    "healthy",
                    "pipeline cycle completed successfully",
                    cycle=cycle_number,
                    failed_steps=[],
                    consecutive_failed_cycles=0,
                )
        next_run_at = _next_trigger_after(
            next_run_at, interval_seconds, time.monotonic()
        )

    if health is not None:
        health.update("stopping", "pipeline scheduler stopping", cycle=cycle_number)
    _log("pipeline scheduler stopped")
    return 0


def _run_cycle(
    cycle_number: int,
    stop_event: Event,
    *,
    health: HealthReporter | None = None,
) -> list[str]:
    started_at = time.monotonic()
    failed_steps: list[str] = []
    blocking_succeeded = True
    previous_name = "cycle start"
    _log(f"cycle={cycle_number} started")
    if health is not None:
        cycle_state = "unhealthy" if health.state == "unhealthy" else "running"
        health.update(
            cycle_state,
            "pipeline cycle running",
            cycle=cycle_number,
            failed_steps=[],
        )

    for index, step in enumerate(PIPELINE_STEPS, start=1):
        if stop_event.is_set():
            break
        _log(
            f"cycle={cycle_number} transition={previous_name!r}->{step.name!r} "
            f"step={index}/{len(PIPELINE_STEPS)}"
        )
        extra_env: dict[str, str] = {}
        if step.key == "image-matching":
            extra_env[SKIP_IMAGE_MATCHING_ENV] = "0" if blocking_succeeded else "1"
            if not blocking_succeeded:
                _log(
                    f"cycle={cycle_number} step={step.name!r} will no-op because "
                    "image blocking failed",
                    level=logging.ERROR,
                )

        if health is not None:
            step_state = "unhealthy" if failed_steps else cycle_state
            health.update(
                step_state,
                f"pipeline step running: {step.name}",
                cycle=cycle_number,
                stage=step.key,
                failed_steps=failed_steps,
            )
        if health is None:
            return_code = _run_step(step, stop_event, extra_env=extra_env)
        else:
            return_code = _run_step(
                step,
                stop_event,
                extra_env=extra_env,
                health=health,
                cycle_number=cycle_number,
            )
        if return_code != 0:
            failed_steps.append(step.name)
            _log(
                f"cycle={cycle_number} step={step.name!r} failed "
                f"exit_code={return_code}; continuing",
                level=logging.ERROR,
            )
            if health is not None:
                health.update(
                    "unhealthy",
                    f"pipeline step failed: {step.name}",
                    cycle=cycle_number,
                    stage=step.key,
                    failed_steps=failed_steps,
                    exit_code=return_code,
                )
        if step.key == "image-blocking":
            blocking_succeeded = return_code == 0
        previous_name = step.name

    elapsed = time.monotonic() - started_at
    status = "success" if not failed_steps else f"failed_steps={failed_steps}"
    _log(
        f"cycle={cycle_number} finished duration={elapsed:.1f}s status={status}",
        level=logging.ERROR if failed_steps else logging.INFO,
    )
    return failed_steps


def _run_step(
    step: PipelineStep,
    stop_event: Event,
    *,
    extra_env: Mapping[str, str] | None = None,
    health: HealthReporter | None = None,
    cycle_number: int | None = None,
) -> int:
    env = os.environ.copy()
    env.update(extra_env or {})
    baseline_child_ids = _child_process_ids(os.getpid())
    started_at = time.monotonic()
    _log(f"step={step.name!r} started command={list(step.command)!r}")
    try:
        process = subprocess.Popen(
            step.command,
            cwd=step.cwd,
            env=env,
            start_new_session=True,
        )
    except OSError:
        _log(
            f"step={step.name!r} could not start",
            level=logging.ERROR,
            exc_info=True,
        )
        return 127
    while process.poll() is None:
        if health is not None:
            health.heartbeat(cycle=cycle_number, stage=step.key)
        if stop_event.wait(POLL_SECONDS):
            _stop_process(process)
            break
    return_code = process.wait()
    try:
        _stop_lingering_process_group(process.pid)
    finally:
        _stop_new_child_processes(baseline_child_ids)
    _log(
        f"step={step.name!r} finished exit_code={return_code} "
        f"duration={time.monotonic() - started_at:.1f}s"
    )
    return return_code


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    _log("terminating active pipeline step")
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _log(
            "active pipeline step did not stop in time; killing it",
            level=logging.ERROR,
        )
        _signal_process_group(process, signal.SIGKILL)
        process.wait()


def _stop_lingering_process_group(process_group_id: int) -> None:
    if not _process_group_exists(process_group_id):
        return
    _log(f"terminating lingering process group pgid={process_group_id}")
    _signal_process_group_id(process_group_id, signal.SIGTERM)
    if _wait_for_process_group_exit(process_group_id, SHUTDOWN_TIMEOUT_SECONDS):
        return
    _log(
        f"lingering process group did not stop in time; killing pgid={process_group_id}",
        level=logging.ERROR,
    )
    _signal_process_group_id(process_group_id, signal.SIGKILL)
    if not _wait_for_process_group_exit(process_group_id, SHUTDOWN_TIMEOUT_SECONDS):
        raise RuntimeError(f"process group {process_group_id} survived SIGKILL")


def _wait_for_process_group_exit(process_group_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _process_group_exists(process_group_id):
        _reap_process_group_children(process_group_id)
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)
    return True


def _reap_process_group_children(process_group_id: int) -> None:
    while True:
        try:
            child_pid, _status = os.waitpid(-process_group_id, os.WNOHANG)
        except ChildProcessError:
            return
        if child_pid == 0:
            return


def _process_group_exists(process_group_id: int) -> bool:
    if _PROC_ROOT.is_dir():
        return _process_group_exists_in_proc(process_group_id)
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_group_exists_in_proc(process_group_id: int) -> bool:
    return any(
        item.process_group_id == process_group_id and item.state != "Z"
        for item in _iter_proc_stats()
    )


def _stop_new_child_processes(baseline_child_ids: set[int]) -> None:
    if not _new_child_process_ids(baseline_child_ids):
        return
    _log("terminating detached pipeline-stage descendants")
    if _signal_and_wait_for_new_children(
        baseline_child_ids, signal.SIGTERM, SHUTDOWN_TIMEOUT_SECONDS
    ):
        return
    _log(
        "detached descendants did not stop in time; killing them",
        level=logging.ERROR,
    )
    if not _signal_and_wait_for_new_children(
        baseline_child_ids, signal.SIGKILL, SHUTDOWN_TIMEOUT_SECONDS
    ):
        raise RuntimeError("pipeline-stage descendants survived SIGKILL")


def _signal_and_wait_for_new_children(
    baseline_child_ids: set[int], sig: signal.Signals, timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while child_ids := _new_child_process_ids(baseline_child_ids):
        for process_id in child_ids:
            _signal_process(process_id, sig)
            _reap_process(process_id)
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)
    return True


def _new_child_process_ids(baseline_child_ids: set[int]) -> set[int]:
    return _child_process_ids(os.getpid()) - baseline_child_ids


def _child_process_ids(parent_process_id: int) -> set[int]:
    return {
        item.process_id
        for item in _iter_proc_stats()
        if item.parent_process_id == parent_process_id
    }


def _iter_proc_stats():
    for stat_path in _PROC_ROOT.glob("[0-9]*/stat"):
        try:
            parsed = _parse_proc_stat(stat_path.read_text())
        except OSError:
            continue
        if parsed is not None:
            yield parsed


def _parse_proc_stat(value: str) -> ProcStat | None:
    opening = value.find("(")
    fields = value[value.rfind(")") + 2 :].split()
    if opening <= 0 or len(fields) < 3:
        return None
    try:
        return ProcStat(
            process_id=int(value[:opening].strip()),
            state=fields[0],
            parent_process_id=int(fields[1]),
            process_group_id=int(fields[2]),
        )
    except ValueError:
        return None


def _set_child_subreaper(enabled: bool) -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("pipeline scheduler requires Linux process supervision")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, int(enabled), 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _signal_process(process_id: int, sig: signal.Signals) -> None:
    try:
        os.kill(process_id, sig)
    except ProcessLookupError:
        return


def _reap_process(process_id: int) -> None:
    try:
        os.waitpid(process_id, os.WNOHANG)
    except ChildProcessError:
        return


def _signal_process_group(
    process: subprocess.Popen[bytes], sig: signal.Signals
) -> None:
    _signal_process_group_id(process.pid, sig)


def _signal_process_group_id(process_group_id: int, sig: signal.Signals) -> None:
    try:
        os.killpg(process_group_id, sig)
    except ProcessLookupError:
        return


def _next_trigger_after(previous: float, interval: float, now: float) -> float:
    next_run = previous + interval
    while next_run <= now:
        next_run += interval
    return next_run


def _log(
    message: str,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
) -> None:
    logger.log(level, message, exc_info=exc_info)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
