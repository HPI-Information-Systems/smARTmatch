"""Launch and reap one-shot scraper workers from the dashboard process."""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_MAX_OUTSTANDING = 8


class WorkerProcessLauncher:
    """Submit fixed worker commands without loading scrapers into Waitress."""

    def __init__(
        self,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        *,
        max_outstanding: int = _DEFAULT_MAX_OUTSTANDING,
    ) -> None:
        if max_outstanding < 1:
            raise ValueError("max_outstanding must be positive")
        self._popen_factory = popen_factory
        self._max_outstanding = max_outstanding
        self._lock = threading.Lock()
        self._children: dict[str, Any | None] = {}

    def launch_scraper(self, scraper_name: str) -> dict[str, Any]:
        return self._launch(
            [
                sys.executable,
                "-m",
                "scrapers.worker",
                "run",
                scraper_name,
                "--source",
                "manual",
            ]
        )

    def launch_all(self) -> dict[str, Any]:
        return self._launch(
            [
                sys.executable,
                "-m",
                "scrapers.worker",
                "run-all",
                "--source",
                "manual",
            ]
        )

    def _launch(self, command: Sequence[str]) -> dict[str, Any]:
        request_id = str(uuid4())
        with self._lock:
            if len(self._children) >= self._max_outstanding:
                raise OSError("Too many scraper worker requests are still active")
            # Reserve capacity before Popen so concurrent Waitress requests
            # cannot all pass the limit at once.
            self._children[request_id] = None

        try:
            process = self._popen_factory(list(command), cwd=str(_REPO_ROOT))
        except Exception:
            with self._lock:
                self._children.pop(request_id, None)
            raise

        with self._lock:
            self._children[request_id] = process

        watcher = threading.Thread(
            target=self._wait_and_forget,
            args=(request_id, process),
            name=f"scraper-worker-reaper-{request_id}",
            daemon=True,
        )
        try:
            watcher.start()
        except Exception as exc:
            with self._lock:
                self._children.pop(request_id, None)
            process.terminate()
            process.wait()
            raise OSError(f"Could not start scraper worker reaper: {exc}") from exc
        return {
            "request_id": request_id,
            "pid": process.pid,
            "status": "submitted",
        }

    def _wait_and_forget(self, request_id: str, process: Any) -> None:
        return_code: int | str = "wait-error"
        try:
            return_code = process.wait()
        except Exception as exc:
            print(
                f"[dashboard] worker request={request_id} pid={process.pid} "
                f"wait failed: {exc}",
                flush=True,
            )
        finally:
            with self._lock:
                self._children.pop(request_id, None)
        print(
            f"[dashboard] worker request={request_id} pid={process.pid} "
            f"exited={return_code}",
            flush=True,
        )
