"""Canonical identifiers, JSON, hashes, and timestamps for sync."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID


def _canonical_uuid(value: object) -> str:
    return str(UUID(str(value)))


def _values(rows: Any, key: str) -> set[str]:
    return {
        str(value)
        for value in (row.get(key) for row in rows)
        if value is not None and str(value)
    }


def _match_key(lost_id: str, auction_id: str) -> str:
    return f"{lost_id}:{auction_id}"


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return _isoformat(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _isoformat(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")
