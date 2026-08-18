"""Small serialization and database-row conversion helpers."""

import math
from datetime import date, datetime, timezone
from typing import Any, Mapping


def _fetchone_dict(cur) -> dict[str, Any]:
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("Telemetry query returned no row")
    return dict(zip(_column_names(cur), row))


def _fetchall_dicts(cur) -> list[dict[str, Any]]:
    names = _column_names(cur)
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _column_names(cur) -> list[str]:
    return [
        column.name if hasattr(column, "name") else column[0]
        for column in cur.description
    ]


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return _isoformat(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _isoformat(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")
