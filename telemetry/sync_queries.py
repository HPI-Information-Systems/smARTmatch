"""Bounded PostgreSQL snapshot and entity-loading queries."""

from typing import Any, Sequence
from uuid import UUID

from matching_pipeline.shared.db import connect
from telemetry.sync_budget import _ClosureMaterializationBudget
from telemetry.sync_utils import _canonical_uuid


def _reserve_query_rows(
    conn,
    statement: str,
    parameters: tuple[Any, ...],
    budget: _ClosureMaterializationBudget,
    *,
    label: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(statement, parameters)
        result = cur.fetchone()
    payload_bytes, row_count = result if result is not None else (0, 0)
    budget.reserve(payload_bytes or 0, row_count or 0, label=label)


def _fetch_requested_match_rows(
    conn,
    pairs: Sequence[tuple[str, str]],
    *,
    materialization_budget: _ClosureMaterializationBudget | None = None,
) -> list[dict[str, Any]]:
    if not pairs:
        return []
    lost_ids = [UUID(_canonical_uuid(lost_id)) for lost_id, _auction_id in pairs]
    auction_ids = [UUID(_canonical_uuid(auction_id)) for _lost_id, auction_id in pairs]
    parameters = (lost_ids, auction_ids)
    if materialization_budget is not None:
        _reserve_query_rows(
            conn,
            """
            WITH requested(lost_id, auction_id) AS (
                SELECT * FROM unnest(%s::uuid[], %s::uuid[])
            )
            SELECT
                COALESCE(SUM(octet_length(to_jsonb(ms)::text)), 0),
                COUNT(*)
            FROM requested
            JOIN match_score ms USING (lost_id, auction_id)
            """,
            parameters,
            materialization_budget,
            label="match_score rows",
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH requested(lost_id, auction_id) AS (
                SELECT * FROM unnest(%s::uuid[], %s::uuid[])
            )
            SELECT to_jsonb(ms)
            FROM requested
            JOIN match_score ms USING (lost_id, auction_id)
            ORDER BY ms.lost_id, ms.auction_id
            """,
            parameters,
        )
        return [dict(row[0]) for row in cur.fetchall()]


def _snapshot_connection():
    conn = connect(connect_timeout=10)
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cur.execute("SET LOCAL statement_timeout = '110min'")
        cur.execute("SET LOCAL lock_timeout = '10s'")
        cur.execute("SET LOCAL TIME ZONE 'UTC'")
        cur.execute("SET LOCAL extra_float_digits = 3")
    return conn


def _fetch_entities(
    conn,
    table: str,
    primary_key: str,
    ids: Sequence[str] | set[str],
    *,
    materialization_budget: _ClosureMaterializationBudget | None = None,
) -> dict[str, dict[str, Any]]:
    allowed = {
        ("location", "location_id"),
        ("artist", "artist_id"),
        ("institution", "institution_id"),
        ("literature_source", "literature_id"),
        ("auction_platform", "auction_platform_id"),
        ("auctioneer", "auctioneer_id"),
        ("expert", "expert_id"),
        ("matching_program", "matching_program_id"),
        ("lost_artwork", "lost_artwork_id"),
        ("auction_artwork", "auction_artwork_id"),
    }
    if (table, primary_key) not in allowed:
        raise ValueError(f"Unsupported telemetry entity table: {table}.{primary_key}")
    values = sorted(
        {UUID(_canonical_uuid(value)) for value in ids if value is not None},
        key=str,
    )
    if not values:
        return {}
    parameters = (values,)
    if materialization_budget is not None:
        _reserve_query_rows(
            conn,
            f"""
            SELECT
                COALESCE(SUM(octet_length(to_jsonb(row_data)::text)), 0),
                COUNT(*)
            FROM {table} row_data
            WHERE {primary_key} = ANY(%s::uuid[])
            """,
            parameters,
            materialization_budget,
            label=f"{table} rows",
        )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {primary_key}::text, to_jsonb(row_data)
            FROM {table} row_data
            WHERE {primary_key} = ANY(%s::uuid[])
            ORDER BY {primary_key}
            """,
            parameters,
        )
        return {str(key): dict(row) for key, row in cur.fetchall()}


def _fetch_integer_entities(
    conn,
    ids: set[int],
    *,
    materialization_budget: _ClosureMaterializationBudget | None = None,
) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    parameters = (sorted(ids),)
    if materialization_budget is not None:
        _reserve_query_rows(
            conn,
            """
            SELECT
                COALESCE(SUM(octet_length(to_jsonb(row_data)::text)), 0),
                COUNT(*)
            FROM image_file row_data
            WHERE image_file_id = ANY(%s)
            """,
            parameters,
            materialization_budget,
            label="image_file rows",
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT image_file_id::text, to_jsonb(row_data)
            FROM image_file row_data
            WHERE image_file_id = ANY(%s)
            ORDER BY image_file_id
            """,
            parameters,
        )
        return {str(key): dict(row) for key, row in cur.fetchall()}


def _fetch_link_rows(
    conn,
    table: str,
    artwork_id_column: str,
    artwork_ids: set[str],
    *,
    materialization_budget: _ClosureMaterializationBudget | None = None,
) -> list[dict[str, Any]]:
    allowed = {
        ("lost_artwork_image_file", "lost_artwork_id"),
        ("auction_artwork_image_file", "auction_artwork_id"),
    }
    if (table, artwork_id_column) not in allowed:
        raise ValueError(f"Unsupported telemetry link table: {table}")
    if not artwork_ids:
        return []
    values = sorted(
        {UUID(_canonical_uuid(value)) for value in artwork_ids},
        key=str,
    )
    parameters = (values,)
    if materialization_budget is not None:
        _reserve_query_rows(
            conn,
            f"""
            SELECT
                COALESCE(SUM(octet_length(to_jsonb(row_data)::text)), 0),
                COUNT(*)
            FROM {table} row_data
            WHERE {artwork_id_column} = ANY(%s::uuid[])
            """,
            parameters,
            materialization_budget,
            label=f"{table} rows",
        )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT to_jsonb(row_data)
            FROM {table} row_data
            WHERE {artwork_id_column} = ANY(%s::uuid[])
            ORDER BY {artwork_id_column}, image_file_id
            """,
            parameters,
        )
        return [dict(row[0]) for row in cur.fetchall()]
