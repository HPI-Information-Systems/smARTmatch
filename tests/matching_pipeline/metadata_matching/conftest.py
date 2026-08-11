# run all matching test scripts with: python -m pytest -q tests/matching
from __future__ import annotations

import csv
import sys
import types
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# The production code imports psycopg at module import time. Unit tests should not
# require a real database driver unless the deployment/integration test explicitly
# provides one. A test can monkeypatch psycopg.connect on the imported module.
if "psycopg" not in sys.modules:
    psycopg_stub = types.ModuleType("psycopg")

    class Connection:  # pragma: no cover - only used for annotations at import time
        pass

    def connect(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Unexpected real database connection in unit test")

    psycopg_stub.Connection = Connection
    psycopg_stub.connect = connect
    sys.modules["psycopg"] = psycopg_stub


def load_fixture_csv(file_name: str) -> list[dict[str, str]]:
    with (FIXTURES_DIR / file_name).open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def parse_pg_array(value: str | None) -> list[str]:
    if value is None:
        return []
    stripped = str(value).strip()
    if stripped in {"", "NULL", "{}"}:
        return []
    if stripped.startswith("{") and stripped.endswith("}"):
        stripped = stripped[1:-1]
    return [part.strip() for part in stripped.split(",") if part.strip()]


def nullable_float(value: str | None) -> float | None:
    if value is None or str(value).strip() in {"", "NULL"}:
        return None
    return float(value)


def nullable_int(value: str | None) -> int | None:
    if value is None or str(value).strip() in {"", "NULL"}:
        return None
    return int(value)


def lost_db_row(row: dict[str, str]) -> tuple[Any, ...]:
    # Shape must match the lost_artwork SELECT in main_matcher/metadata_matcher.py's
    # match_metadata(): only dict_material_name/dict_technique_name are fetched at
    # matching time, not the raw material/technique text (that's normalization's job).
    return (
        row["lost_artwork_id"],
        row["title"],
        parse_pg_array(row["dict_material_name"]),
        parse_pg_array(row["dict_technique_name"]),
        nullable_float(row["width"]),
        nullable_float(row["height"]),
        nullable_int(row["dating_start"]),
        nullable_int(row["dating_end"]),
        [row["artist_raw_data"]] if row.get("artist_raw_data") else [],
    )


def auction_db_row(row: dict[str, str]) -> tuple[Any, ...]:
    # Shape must match the auction_artwork SELECT in main_matcher/metadata_matcher.py's
    # match_metadata(): only dict_material_name/dict_technique_name are fetched at
    # matching time, not the raw material/technique text (that's normalization's job).
    return (
        row["spsg_artwork_id"],
        row["artist_raw_data"],
        row["title"],
        parse_pg_array(row["dict_material_name"]),
        parse_pg_array(row["dict_technique_name"]),
        nullable_float(row["width"]),
        nullable_float(row["height"]),
        nullable_int(row["dating_start"]),
        nullable_int(row["dating_end"]),
    )
