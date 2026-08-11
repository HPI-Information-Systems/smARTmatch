"""Small file-backed runtime settings shared by container sibling processes."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from uuid import uuid4

_RUNTIME_CONFIG_ENV = "SCRAPER_RUNTIME_CONFIG_PATH"
_DEFAULT_RUNTIME_CONFIG_PATH = "/tmp/smartmatch-scraper-runtime.json"
_WRITE_LOCK = threading.Lock()


def _config_path() -> Path:
    return Path(os.getenv(_RUNTIME_CONFIG_ENV, _DEFAULT_RUNTIME_CONFIG_PATH))


def load_request_cooldown_override() -> object | None:
    """Return the stored cooldown value, or ``None`` when no override exists."""
    path = _config_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid scraper runtime config at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid scraper runtime config at {path}: expected an object")
    return payload.get("request_cooldown_seconds")


def save_request_cooldown_override(seconds: float) -> None:
    """Atomically persist a cooldown override for future worker processes."""
    path = _config_path()
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps({"request_cooldown_seconds": seconds}),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
