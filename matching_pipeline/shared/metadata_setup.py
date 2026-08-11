"""One-time DB setup: seed dict_material, dict_technique, variants, and matching_program."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import psycopg

from matching_pipeline.shared.db import connect as db_connect

logger = logging.getLogger(__name__)

_CSV_DIR = Path(__file__).resolve().parent / "reference_data"

_MATCHING_PROGRAM_ID = "1e3297fc-ee74-4498-a4d3-ac98ddb8c70d"
_MATCHING_PROGRAM_NAME = "main_matcher"
_MATCHING_PROGRAM_VERSION = "1.0"
_MATCHING_PROGRAM_DESCRIPTION = (
    "renorm weights:35, 25, 15, 15, 5, 5; Jaro-Winkler on artist, "
    "idf_common_title matching on title, binary overlap on dating and dimensions, "
    "material and technique with dictionary normalization. None value handling."
)


def _db_connect() -> psycopg.Connection:
    return db_connect()


def _read_csv(path: Path) -> list[dict[str, str | None]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        return [
            {k: (v.strip() or None) for k, v in row.items()}
            for row in reader
        ]

# insert hierarchy rows in topological order (parents before children).
def _insert_hierarchy(
    cur: psycopg.Cursor,
    table: str,
    name_col: str,
    parent_col: str,
    rows: list[dict[str, str | None]],
) -> int:
    cur.execute(f"SELECT {name_col} FROM {table}")
    loaded: set[str] = {r[0] for r in cur.fetchall()}

    pending = [(r[name_col], r[parent_col]) for r in rows if r[name_col]]
    inserted = 0

    while pending:
        ready = [(name, parent) for name, parent in pending if parent is None or parent in loaded]
        if not ready:
            unresolved = [name for name, _ in pending[:5]]
            raise RuntimeError(f"Hierarchy cycle or missing parent in {table}: {unresolved}")

        for name, parent in ready:
            cur.execute(
                f"""
                INSERT INTO {table} ({name_col}, {parent_col})
                VALUES (%s, %s)
                ON CONFLICT ({name_col})
                DO UPDATE SET {parent_col} = EXCLUDED.{parent_col}
                WHERE {table}.{parent_col} IS DISTINCT FROM EXCLUDED.{parent_col}
                """,
                (name, parent),
            )
            inserted += cur.rowcount
            loaded.add(name)  # type: ignore[arg-type]

        pending = [(n, p) for n, p in pending if n not in loaded]

    return inserted


def _insert_variants(
    cur: psycopg.Cursor,
    table: str,
    dict_col: str,
    raw_col: str,
    unique_index: str,
    rows: list[dict[str, str | None]],
) -> int:
    cur.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {unique_index} ON {table} ({dict_col}, {raw_col})"
    )
    inserted = 0
    for row in rows:
        dict_name, raw = row.get(dict_col), row.get(raw_col)
        if not dict_name or not raw:
            continue
        cur.execute(
            f"""
            INSERT INTO {table} ({dict_col}, {raw_col})
            VALUES (%s, %s)
            ON CONFLICT ({dict_col}, {raw_col}) DO NOTHING
            """,
            (dict_name, raw),
        )
        inserted += cur.rowcount
    return inserted


def run_setup_once() -> None:
    conn = _db_connect()
    try:
        logger.info("Running one-time DB setup...")

        with conn.cursor() as cur:
            rows = _read_csv(_CSV_DIR / "hierarchy_material.csv")
            n = _insert_hierarchy(cur, "dict_material", "material_name", "material_parent", rows)
            logger.info(f"dict_material: {n} rows inserted/updated")

            rows = _read_csv(_CSV_DIR / "hierarchy_technique.csv")
            n = _insert_hierarchy(cur, "dict_technique", "technique_name", "technique_parent", rows)
            logger.info(f"dict_technique: {n} rows inserted/updated")

            rows = _read_csv(_CSV_DIR / "material_variants.csv")
            n = _insert_variants(
                cur, "material_variant", "dict_material_name", "material_raw_data",
                "uq_material_variant_name_raw", rows,
            )
            logger.info(f"material_variant: {n} rows inserted")

            rows = _read_csv(_CSV_DIR / "technique_variants.csv")
            n = _insert_variants(
                cur, "technique_variant", "dict_technique_name", "technique_raw_data",
                "uq_technique_variant_name_raw", rows,
            )
            logger.info(f"technique_variant: {n} rows inserted")

            cur.execute(
                """
                INSERT INTO matching_program (matching_program_id, name, version, description)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (_MATCHING_PROGRAM_ID, _MATCHING_PROGRAM_NAME, _MATCHING_PROGRAM_VERSION, _MATCHING_PROGRAM_DESCRIPTION),
            )
            logger.info(f"matching_program: seeded id={_MATCHING_PROGRAM_ID}")

        conn.commit()
        logger.info("One-time DB setup complete.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
