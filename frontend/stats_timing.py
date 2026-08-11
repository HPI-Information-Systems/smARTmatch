"""Timing helpers for frontend statistics aggregation diagnostics."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class TimingEntry:
    """Single measured aggregation step."""

    label: str
    elapsed_ms: float


class StatsTimingCollector:
    """Collect elapsed time measurements for one stats aggregation run."""

    def __init__(self, clock=time.perf_counter):
        self._clock = clock
        self._entries = []

    @contextmanager
    def measure(self, label):
        start = self._clock()
        try:
            yield
        finally:
            self.add(label, self._clock() - start)

    def add(self, label, elapsed_seconds):
        self._entries.append(TimingEntry(label, elapsed_seconds * 1000))

    def format_message(self):
        return ", ".join(
            f"{entry.label}: {entry.elapsed_ms:.1f} ms" for entry in self._entries
        )
