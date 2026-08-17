"""Atomic work-aware health status files for long-running SmartMatch services."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from shared.logging_adapter import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 1
HEALTHY_STATES = frozenset({"starting", "running", "healthy", "degraded", "disabled"})
KNOWN_STATES = HEALTHY_STATES | {"unhealthy", "stopping"}
DEFAULT_MAX_AGE_SECONDS = 180.0
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0
MAX_STATUS_BYTES = 64 * 1024


class HealthReporter:
    """Write a bounded status document without making daemon work depend on I/O."""

    def __init__(
        self,
        service: str,
        path: Path,
        *,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self.service = service
        self.path = path
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.state = "starting"
        self._detail = "initializing"
        self._fields: dict[str, Any] = {}
        self._last_write_monotonic = float("-inf")

    @classmethod
    def from_environment(cls, service: str) -> "HealthReporter":
        default_path = f"/tmp/smartmatch-{service.replace('_', '-')}-health.json"
        return cls(service, Path(os.getenv("SMARTMATCH_HEALTH_FILE", default_path)))

    def update(self, state: str, detail: str, **fields: Any) -> bool:
        if state not in KNOWN_STATES:
            raise ValueError(f"Unknown health state: {state}")
        self.state = state
        self._detail = str(detail)[:2000]
        self._fields = _bounded_fields(fields)
        return self._write()

    def heartbeat(self, **fields: Any) -> bool:
        if (
            time.monotonic() - self._last_write_monotonic
            < self.heartbeat_interval_seconds
        ):
            return True
        if fields:
            self._fields.update(_bounded_fields(fields))
        return self._write()

    def _write(self) -> bool:
        now = datetime.now(timezone.utc)
        document = {
            "schema_version": SCHEMA_VERSION,
            "service": self.service,
            "state": self.state,
            "detail": self._detail,
            "updated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "updated_at_epoch": now.timestamp(),
            **self._fields,
        }
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_STATUS_BYTES:
            logger.error(
                "Health status exceeds limit service=%s bytes=%d",
                self.service,
                len(encoded),
            )
            return False
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=self.path.parent
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o644)
            os.replace(temporary_path, self.path)
            self._last_write_monotonic = time.monotonic()
            return True
        except OSError:
            logger.exception(
                "Could not write health status service=%s path=%s",
                self.service,
                self.path,
            )
            return False
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _bounded_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in fields.items():
        key_text = str(key)[:100]
        if value is None or isinstance(value, (bool, int)):
            result[key_text] = value
        elif isinstance(value, float):
            result[key_text] = value if math.isfinite(value) else str(value)
        elif isinstance(value, str):
            result[key_text] = value[:2000]
        elif isinstance(value, (list, tuple)):
            result[key_text] = [str(item)[:200] for item in value[:100]]
        else:
            result[key_text] = str(value)[:2000]
    return result


def read_health_status(
    path: Path,
    *,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    now_epoch: float | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Return probe status, diagnostic detail, and the validated document."""
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        return False, "max age must be a positive finite number", None
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_STATUS_BYTES + 1)
    except OSError as exc:
        return False, f"health status is unavailable: {type(exc).__name__}", None
    if len(raw) > MAX_STATUS_BYTES:
        return False, "health status exceeds the size limit", None
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, "health status is not valid UTF-8 JSON", None
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
    ):
        return False, "health status has an unsupported schema", None
    if document["schema_version"] != SCHEMA_VERSION:
        return False, "health status has an unsupported schema", None
    if not isinstance(document.get("service"), str) or not document["service"].strip():
        return False, "health status has no valid service", document
    if not isinstance(document.get("detail"), str):
        return False, "health status has no valid detail", document
    if (
        not isinstance(document.get("updated_at"), str)
        or not document["updated_at"].strip()
    ):
        return False, "health status has no valid display timestamp", document
    state = document.get("state")
    if not isinstance(state, str) or state not in KNOWN_STATES:
        return False, "health status has an unknown state", document
    updated_at = document.get("updated_at_epoch")
    if (
        isinstance(updated_at, bool)
        or not isinstance(updated_at, (int, float))
        or not math.isfinite(updated_at)
    ):
        return False, "health status has no valid timestamp", document
    now = time.time() if now_epoch is None else now_epoch
    age = now - float(updated_at)
    if age < -60:
        return False, "health status timestamp is in the future", document
    if age > max_age_seconds:
        return False, f"health status is stale ({age:.1f}s)", document
    if state not in HEALTHY_STATES:
        detail = str(document.get("detail") or state)
        return False, f"service state is {state}: {detail}", document
    return True, f"service state is {state}", document


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument(
        "--max-age-seconds", type=float, default=DEFAULT_MAX_AGE_SECONDS
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    healthy, message, document = read_health_status(
        args.file, max_age_seconds=args.max_age_seconds
    )
    stream = sys.stdout if healthy else sys.stderr
    print(message, file=stream)
    if document is not None:
        print(json.dumps(document, sort_keys=True), file=stream)
    return 0 if healthy else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
