"""Supervisor event listener that fails the container if an essential service dies."""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Callable

_ESSENTIAL_PROCESSES = {"scraper-dashboard", "scraper-scheduler"}


def _payload_fields(payload: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in payload.split():
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        fields[key] = value
    return fields


def handle_event(
    event_name: str,
    payload: str,
    *,
    parent_pid: int,
    kill: Callable[[int, int], None] = os.kill,
) -> bool:
    """Stop supervisord when a dashboard or scheduler reaches FATAL."""
    process_name = _payload_fields(payload).get("processname")
    if event_name != "PROCESS_STATE_FATAL" or process_name not in _ESSENTIAL_PROCESSES:
        return False
    print(
        f"[supervisor] essential process {process_name} entered FATAL; "
        "stopping container",
        file=sys.stderr,
        flush=True,
    )
    kill(parent_pid, signal.SIGTERM)
    return True


def main() -> int:
    while True:
        sys.stdout.write("READY\n")
        sys.stdout.flush()
        header_line = sys.stdin.readline()
        if not header_line:
            return 0
        headers = _payload_fields(header_line)
        try:
            payload_length = int(headers["len"])
        except (KeyError, ValueError):
            return 2
        payload = sys.stdin.read(payload_length)
        handle_event(
            headers.get("eventname", ""),
            payload,
            parent_pid=os.getppid(),
        )
        sys.stdout.write("RESULT\n2\nOK")
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
