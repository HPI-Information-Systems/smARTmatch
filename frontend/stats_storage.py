"""Image-file storage statistics for the SmartMatch frontend."""

from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Lock, Thread

from .stats_format import format_bytes, format_int


_SCAN_CACHE_LOCK = Lock()
_SCAN_CACHE = {
    "signature": None,
    "disk_bytes": None,
    "missing_count": None,
    "expires_at": 0.0,
    "refreshing": False,
}
_UNKNOWN_DISK_LABEL = "xx GB"


def _cache_ttl_seconds():
    value = os.getenv("SMARTMATCH_STATS_CACHE_TTL_SECONDS", "60")
    try:
        seconds = int(value)
    except ValueError as exc:
        raise ValueError(
            "Environment variable SMARTMATCH_STATS_CACHE_TTL_SECONDS must be an integer"
        ) from exc
    return max(0, seconds)


def _image_root():
    root = Path(os.getenv("SMARTMATCH_IMAGES_DIR", "db/images")).expanduser()
    return root if root.is_absolute() else Path.cwd() / root


def _resolve_existing_path(raw_path, image_root):
    path_text = str(raw_path or "").strip()
    if not path_text:
        return None
    path = Path(path_text).expanduser()
    candidates = [path] if path.is_absolute() else [Path.cwd() / path, image_root / path]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    return None


def _scan_image_files(path_list, image_root):
    total_bytes = 0
    missing = 0
    seen = set()
    for raw_path in path_list:
        path = _resolve_existing_path(raw_path, image_root)
        if path is None:
            missing += 1
            continue
        if path in seen:
            continue
        try:
            total_bytes += path.stat().st_size
        except OSError:
            missing += 1
            continue
        seen.add(path)
    return total_bytes, missing


def _cache_signature(path_list, image_root):
    paths = sorted(str(path or "") for path in path_list)
    return (str(image_root), tuple(paths))


def _cached_scan_values():
    with _SCAN_CACHE_LOCK:
        return (
            _SCAN_CACHE["disk_bytes"],
            _SCAN_CACHE["missing_count"],
            _SCAN_CACHE["expires_at"],
        )


def _store_scan_values(signature, disk_bytes, missing_count, ttl_seconds):
    with _SCAN_CACHE_LOCK:
        _SCAN_CACHE.update(
            {
                "signature": signature,
                "disk_bytes": disk_bytes,
                "missing_count": missing_count,
                "expires_at": time.monotonic() + ttl_seconds,
                "refreshing": False,
            }
        )


def _mark_scan_failed(signature):
    with _SCAN_CACHE_LOCK:
        if _SCAN_CACHE["signature"] == signature:
            _SCAN_CACHE["refreshing"] = False


def _refresh_scan_cache(path_list, image_root, signature, ttl_seconds):
    try:
        disk_bytes, missing_count = _scan_image_files(path_list, image_root)
    except Exception:
        _mark_scan_failed(signature)
        return
    _store_scan_values(signature, disk_bytes, missing_count, ttl_seconds)


def _schedule_scan_refresh(path_list, image_root, signature, ttl_seconds):
    with _SCAN_CACHE_LOCK:
        if _SCAN_CACHE["refreshing"]:
            return
        _SCAN_CACHE.update({"signature": signature, "refreshing": True})
    Thread(
        target=_refresh_scan_cache,
        args=(list(path_list), image_root, signature, ttl_seconds),
        daemon=True,
    ).start()


def _metrics_result(path_list, disk_bytes, missing, include_scan_paths=False):
    result = {
        "count": len(path_list),
        "count_label": format_int(len(path_list)),
        "disk_bytes": disk_bytes or 0,
        "disk_label": (
            format_bytes(disk_bytes) if disk_bytes is not None else _UNKNOWN_DISK_LABEL
        ),
        "missing_count": missing or 0,
        "missing_label": format_int(missing or 0),
    }
    if include_scan_paths:
        result["_scan_paths"] = tuple(path_list)
    return result


def image_file_metrics(paths, refresh_async=False, include_scan_paths=False):
    """Count DB image paths and sum the size of files present on disk."""
    path_list = list(paths)
    image_root = _image_root()
    signature = _cache_signature(path_list, image_root)
    if refresh_async:
        disk_bytes, missing, expires_at = _cached_scan_values()
        ttl_seconds = _cache_ttl_seconds()
        if disk_bytes is None or time.monotonic() >= expires_at:
            _schedule_scan_refresh(path_list, image_root, signature, ttl_seconds)
        return _metrics_result(path_list, disk_bytes, missing, include_scan_paths)

    total_bytes, missing = _scan_image_files(path_list, image_root)
    return _metrics_result(path_list, total_bytes, missing, include_scan_paths)
