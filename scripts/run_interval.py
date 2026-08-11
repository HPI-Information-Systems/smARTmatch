#!/usr/bin/env python3
"""Run one shell command at fixed minute intervals without overlapping jobs."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

SHUTDOWN_TIMEOUT_SECONDS = 30.0
POLL_SECONDS = 0.5


@dataclass
class ActiveJob:
    process: subprocess.Popen[bytes]
    lock_fd: int


def main() -> int:
    args = _parse_args()
    interval_seconds = args.interval_minutes * 60.0
    stop_event = Event()
    _install_signal_handlers(stop_event)
    return _run_scheduler(args.command, interval_seconds, stop_event)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", help="Shell command to run, passed as one argument.")
    parser.add_argument("interval_minutes", type=float, help="Run interval in minutes.")
    args = parser.parse_args()
    if not args.command.strip():
        parser.error("command must not be empty")
    if args.interval_minutes <= 0:
        parser.error("interval_minutes must be greater than 0")
    return args


def _install_signal_handlers(stop_event: Event) -> None:
    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def _run_scheduler(command: str, interval_seconds: float, stop_event: Event) -> int:
    lock_path = _lock_path(command)
    active_job: ActiveJob | None = None
    next_run_at = time.monotonic()
    _log(
        f"interval runner started: interval={interval_seconds / 60.0:g}m "
        f"lock={lock_path} command={command!r}"
    )

    while not stop_event.is_set():
        active_job = _reap_finished_job(active_job)
        now = time.monotonic()
        if now >= next_run_at:
            if active_job and active_job.process.poll() is None:
                _log("previous job is still running; skipping this trigger")
            else:
                active_job = _start_job(command, lock_path)
            next_run_at = _next_trigger_after(next_run_at, interval_seconds, now)
        stop_event.wait(min(POLL_SECONDS, max(next_run_at - time.monotonic(), 0.0)))

    _log("interval runner stopping")
    _stop_active_job(active_job)
    _log("interval runner stopped")
    return 0


def _start_job(command: str, lock_path: Path) -> ActiveJob | None:
    lock_fd = _try_lock(lock_path)
    if lock_fd is None:
        _log("job lock is held by another process; skipping this trigger")
        return None
    try:
        _log("job started")
        process = subprocess.Popen(
            command,
            shell=True,
            env=os.environ.copy(),
            pass_fds=(lock_fd,),
            start_new_session=True,
        )
    except Exception:
        os.close(lock_fd)
        raise
    return ActiveJob(process=process, lock_fd=lock_fd)


def _reap_finished_job(active_job: ActiveJob | None) -> ActiveJob | None:
    if active_job is None:
        return None
    return_code = active_job.process.poll()
    if return_code is None:
        return active_job
    _close_lock(active_job.lock_fd)
    if return_code == 0:
        _log("job finished successfully")
    else:
        _log(f"job failed with exit code {return_code}; scheduler will continue")
    return None


def _stop_active_job(active_job: ActiveJob | None) -> None:
    if active_job is None:
        return
    process = active_job.process
    if process.poll() is None:
        _log("terminating active job")
        _terminate_process_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _log("active job did not stop in time; killing it")
            _terminate_process_group(process, signal.SIGKILL)
            process.wait()
    _close_lock(active_job.lock_fd)


def _terminate_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    except OSError:
        if sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()


def _try_lock(lock_path: Path) -> int | None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return None
    os.write(lock_fd, str(os.getpid()).encode("ascii"))
    os.ftruncate(lock_fd, os.lseek(lock_fd, 0, os.SEEK_CUR))
    return lock_fd


def _close_lock(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def _lock_path(command: str) -> Path:
    command_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
    return Path("/tmp") / f"run_interval_{command_hash}.lock"


def _next_trigger_after(previous: float, interval: float, now: float) -> float:
    next_run = previous + interval
    while next_run <= now:
        next_run += interval
    return next_run


def _log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{timestamp} {message}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
