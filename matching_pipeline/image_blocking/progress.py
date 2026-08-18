"""Time-based progress logging for image blocking stages."""

from __future__ import annotations

import logging
from threading import Event, Lock, Thread
from time import perf_counter
from types import TracebackType

logger = logging.getLogger(__name__)

PROGRESS_INTERVAL_SECONDS = 20.0


class ImageProgress:
    """Emit aggregate progress on a timer, independent of batch duration."""

    def __init__(
        self,
        stage: str,
        total_images: int,
        *,
        interval_seconds: float = PROGRESS_INTERVAL_SECONDS,
    ) -> None:
        if total_images < 0:
            raise ValueError("total_images must not be negative")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.stage = stage
        self.total_images = total_images
        self.interval_seconds = interval_seconds
        self.started_at = perf_counter()
        self._completed_images = 0
        self._closed = False
        self._lock = Lock()
        self._stop_event = Event()
        self._thread = Thread(
            target=self._heartbeat,
            name=f"blocking-progress-{stage}",
            daemon=True,
        )
        self._thread.start()

    def __enter__(self) -> ImageProgress:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.finish()
        else:
            self.close()

    def update(self, completed_images: int) -> None:
        """Update the counter read by the periodic heartbeat."""

        if completed_images < 0 or completed_images > self.total_images:
            raise ValueError(
                "completed_images must be between zero and total_images"
            )
        with self._lock:
            self._completed_images = completed_images

    def finish(self) -> None:
        """Stop the heartbeat and always log final average throughput."""

        if not self._stop_heartbeat():
            return
        completed_images = self._snapshot()
        elapsed = max(perf_counter() - self.started_at, 0.0)
        logger.info(
            "Blocking stage finished: stage=%s images=%d/%d throughput=%.2f "
            "images/s elapsed=%s",
            self.stage,
            completed_images,
            self.total_images,
            _throughput(completed_images, elapsed),
            _format_duration(elapsed),
        )

    def close(self) -> None:
        """Stop progress logging without reporting successful completion."""

        self._stop_heartbeat()

    def _heartbeat(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            completed_images = self._snapshot()
            elapsed = max(perf_counter() - self.started_at, 0.0)
            throughput = _throughput(completed_images, elapsed)
            logger.info(
                "Blocking progress: stage=%s images=%d/%d throughput=%.2f "
                "images/s eta=%s elapsed=%s",
                self.stage,
                completed_images,
                self.total_images,
                throughput,
                _eta_text(self.total_images, completed_images, throughput),
                _format_duration(elapsed),
            )

    def _snapshot(self) -> int:
        with self._lock:
            return self._completed_images

    def _stop_heartbeat(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._closed = True
        self._stop_event.set()
        self._thread.join()
        return True


def _throughput(completed_images: int, elapsed_seconds: float) -> float:
    if completed_images <= 0 or elapsed_seconds <= 0:
        return 0.0
    return completed_images / elapsed_seconds


def _eta_text(total_images: int, completed_images: int, throughput: float) -> str:
    if completed_images <= 0 or throughput <= 0:
        return "unknown"
    remaining = max(total_images - completed_images, 0)
    return _format_duration(remaining / throughput)


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{sec:02d}s"
