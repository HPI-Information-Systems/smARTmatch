"""Run scraper batches at a fixed interval inside the scraper container."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

_INTERVAL_ENV = "SCRAPER_INTERVAL"
_INTERVAL_PATTERN = re.compile(r"^([1-9]\d*)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3_600, "d": 86_400}
_REPO_ROOT = Path(__file__).resolve().parents[1]
_POLL_SECONDS = 1.0


@dataclass(frozen=True)
class SchedulerConfig:
    interval_seconds: int


class BatchProcessManager:
    """Launch interval batches without blocking later triggers."""

    def __init__(self, popen_factory: Callable[..., Any] = subprocess.Popen) -> None:
        self._popen_factory = popen_factory
        self._children: list[tuple[str, Any]] = []

    def launch(self, source: str) -> int:
        self.reap_finished()
        command = [
            sys.executable,
            "-m",
            "scrapers.worker",
            "run-all",
            "--source",
            source,
        ]
        process = self._popen_factory(command, cwd=str(_REPO_ROOT))
        self._children.append((source, process))
        print(
            f"[scheduler] source={source} submitted pid={process.pid}",
            flush=True,
        )
        return process.pid

    def reap_finished(self) -> None:
        active: list[tuple[str, Any]] = []
        for source, process in self._children:
            return_code = process.poll()
            if return_code is None:
                active.append((source, process))
                continue
            process.wait()
            print(
                f"[scheduler] source={source} pid={process.pid} "
                f"finished exit_code={return_code}",
                flush=True,
            )
        self._children = active


def parse_interval(value: str) -> int:
    """Parse a positive integer duration such as ``1d``, ``12h``, or ``30m``."""
    normalized = value.strip().lower()
    match = _INTERVAL_PATTERN.fullmatch(normalized)
    if match is None:
        raise ValueError(
            f"{_INTERVAL_ENV} must be a positive duration like 1d, 12h, or 30m; "
            f"got {value!r}"
        )
    amount, unit = match.groups()
    return int(amount) * _UNIT_SECONDS[unit]


def load_config() -> SchedulerConfig:
    """Load the fixed interval, defaulting to one day."""
    return SchedulerConfig(
        interval_seconds=parse_interval(os.getenv(_INTERVAL_ENV, "1d"))
    )


def next_trigger_after(previous: float, interval: float, now: float) -> float:
    """Advance cadence without queueing triggers missed while the process slept."""
    next_trigger = previous + interval
    while next_trigger <= now:
        next_trigger += interval
    return next_trigger


def _submit_batch(manager: BatchProcessManager, source: str) -> None:
    try:
        manager.launch(source)
    except OSError as exc:
        print(
            f"[scheduler] source={source} could not start batch: {exc}",
            file=sys.stderr,
            flush=True,
        )


def run_scheduler(
    config: SchedulerConfig,
    stop_event: Event,
    *,
    manager: BatchProcessManager | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Run immediately, then submit a fresh batch at every interval."""
    process_manager = manager or BatchProcessManager()
    next_trigger = monotonic()
    first_trigger = True
    print(
        f"[scheduler] interval={config.interval_seconds}s; initial batch starts now",
        flush=True,
    )

    while not stop_event.is_set():
        process_manager.reap_finished()
        now = monotonic()
        if now >= next_trigger:
            _submit_batch(
                process_manager,
                "startup" if first_trigger else "scheduled",
            )
            first_trigger = False
            next_trigger = next_trigger_after(
                next_trigger,
                config.interval_seconds,
                now,
            )
            continue
        stop_event.wait(min(_POLL_SECONDS, next_trigger - now))

    process_manager.reap_finished()
    print("[scheduler] stopped", flush=True)
    return 0


def main() -> int:
    try:
        config = load_config()
    except ValueError as exc:
        print(f"[scheduler] invalid configuration: {exc}", file=sys.stderr, flush=True)
        return 2

    stop_event = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return run_scheduler(config, stop_event)


if __name__ == "__main__":
    raise SystemExit(main())
