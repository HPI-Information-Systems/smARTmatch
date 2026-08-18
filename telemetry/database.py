"""PostgreSQL summary queries and durable daily-attempt state."""

import hashlib
from datetime import date, datetime
from typing import Any

from matching_pipeline.shared.db import connect
from telemetry.constants import _DB_ROW_HASH_PREFIX, _HASH_FETCH_ROWS, logger
from telemetry.serialization import _fetchall_dicts, _fetchone_dict, _json_safe
from telemetry.sync_models import SyncDeliveryResult

_EFFECTIVE_MATCH_DATE_SQL = """
CASE
    WHEN ms.metadata_final_score IS NOT NULL
         AND (ms.image_final_score IS NOT NULL OR ms.image_matching_confidence IS NOT NULL)
        THEN GREATEST(ms.metadata_match_date, ms.image_match_date)
    WHEN ms.image_final_score IS NOT NULL OR ms.image_matching_confidence IS NOT NULL
        THEN ms.image_match_date
    ELSE ms.metadata_match_date
END
""".strip()

_COUNTS_SQL = f"""
SELECT
    (SELECT COUNT(*)::bigint FROM lost_artwork) AS lost_artwork_count,
    (SELECT COUNT(*)::bigint FROM auction_artwork) AS auction_artwork_count,
    COUNT(*)::bigint AS all_matches,
    COUNT(*) FILTER (
        WHERE COALESCE(ms.bookmarked, false) = false
          AND COALESCE(ms.rating, 0) = 0
    )::bigint AS new_matches,
    COUNT(*) FILTER (WHERE COALESCE(ms.bookmarked, false) = true)::bigint
        AS bookmarked_matches,
    COUNT(*) FILTER (WHERE COALESCE(ms.rating, 0) > 0)::bigint
        AS accepted_matches,
    COUNT(*) FILTER (WHERE COALESCE(ms.rating, 0) < 0)::bigint
        AS discarded_matches,
    COUNT(*) FILTER (
        WHERE ({_EFFECTIVE_MATCH_DATE_SQL})
            < %s - (%s::double precision * INTERVAL '1 second')
    )::bigint AS expired_matches
FROM match_score ms
"""

_LATEST_APPLIED_MIGRATION_SQL = """
SELECT application_order, migration_name, checksum_sha256, applied_at
FROM public.schema_migrations
WHERE status = 'applied'
ORDER BY application_order DESC
LIMIT 1
"""

_SCRAPER_DATES_SQL = """
WITH scraper_registry(scraper_name, platform_name) AS (
    VALUES
        ('christies', 'Christie''s'),
        ('sothebys', 'sothebys'),
        ('drouot', 'Drouot'),
        ('lottissimo', 'lot-tissimo'),
        ('dorotheum', 'Dorotheum')
)
SELECT
    registry.scraper_name,
    registry.platform_name,
    MAX(artwork.created_at) AS last_artwork_added_at
FROM scraper_registry registry
LEFT JOIN auction_platform platform
    ON lower(platform.name) = lower(registry.platform_name)
LEFT JOIN auction_artwork artwork
    ON artwork.auction_platform_id = platform.auction_platform_id
GROUP BY registry.scraper_name, registry.platform_name
ORDER BY registry.scraper_name
"""

_MATCHING_PROGRAMS_SQL = """
SELECT matching_program_id, name, version
FROM matching_program
ORDER BY name, version, matching_program_id
"""

_HASH_QUERIES = {
    "lost_artwork": """
        SELECT to_jsonb(row_data)::text
        FROM lost_artwork row_data
        ORDER BY lost_artwork_id
    """,
    "artist": """
        SELECT to_jsonb(row_data)::text
        FROM artist row_data
        ORDER BY artist_id
    """,
    "lost_artwork_image_links": """
        SELECT jsonb_build_object(
            'lost_artwork_id', link.lost_artwork_id,
            'image_file_id', link.image_file_id,
            'file_path', image.file_path
        )::text
        FROM lost_artwork_image_file link
        JOIN image_file image ON image.image_file_id = link.image_file_id
        ORDER BY link.lost_artwork_id, link.image_file_id
    """,
    "matching_dictionaries": """
        SELECT value
        FROM (
            SELECT 'dict_material:' || to_jsonb(row_data)::text AS value,
                   material_name AS key1, ''::text AS key2
            FROM dict_material row_data
            UNION ALL
            SELECT 'dict_technique:' || to_jsonb(row_data)::text,
                   technique_name, ''::text
            FROM dict_technique row_data
            UNION ALL
            SELECT 'material_variant:' || to_jsonb(row_data)::text,
                   dict_material_name, material_raw_data
            FROM material_variant row_data
            UNION ALL
            SELECT 'technique_variant:' || to_jsonb(row_data)::text,
                   dict_technique_name, technique_raw_data
            FROM technique_variant row_data
        ) values_to_hash
        ORDER BY value
    """,
}


def _collect_database_snapshot(
    *,
    window_end: datetime,
    expiration_seconds: int,
    conn=None,
) -> dict[str, Any]:
    logger.info("Telemetry database snapshot started")
    owns_connection = conn is None
    conn = conn or connect(connect_timeout=10)
    try:
        if owns_connection:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cur.execute("SET LOCAL statement_timeout = '15min'")
                cur.execute("SET LOCAL lock_timeout = '10s'")
                cur.execute("SET LOCAL TIME ZONE 'UTC'")
                cur.execute("SET LOCAL extra_float_digits = 3")

        table_hashes: dict[str, dict[str, Any]] = {}
        for name, query in _HASH_QUERIES.items():
            logger.info("Telemetry database hashing started dataset=%s", name)
            table_hashes[name] = _hash_database_query(conn, name, query)
            logger.info(
                "Telemetry database hashing complete dataset=%s rows=%d",
                name,
                table_hashes[name]["row_count"],
            )
        logger.info("Telemetry database aggregate queries started")
        with conn.cursor() as cur:
            cur.execute(_COUNTS_SQL, (window_end, expiration_seconds))
            count_row = _fetchone_dict(cur)
            cur.execute(_SCRAPER_DATES_SQL)
            scraper_dates = _fetchall_dicts(cur)
            cur.execute(_MATCHING_PROGRAMS_SQL)
            matching_programs = _fetchall_dicts(cur)
            latest_applied_migration = _latest_applied_migration(cur)
        if owns_connection:
            conn.rollback()
        logger.info("Telemetry database aggregate queries complete")
    finally:
        if owns_connection:
            conn.close()

    lost_hash = table_hashes.pop("lost_artwork")
    return {
        "lost_artwork_hash": lost_hash,
        "dependency_hashes": table_hashes,
        "counts": {
            "lost_artworks": int(count_row["lost_artwork_count"]),
            "auction_artworks": int(count_row["auction_artwork_count"]),
        },
        "match_categories": {
            "all": int(count_row["all_matches"]),
            "new": int(count_row["new_matches"]),
            "bookmarked": int(count_row["bookmarked_matches"]),
            "accepted": int(count_row["accepted_matches"]),
            "expired": int(count_row["expired_matches"]),
            "discarded": int(count_row["discarded_matches"]),
            "bookmarked_is_overlapping": True,
        },
        "scraper_dates": [_json_safe(row) for row in scraper_dates],
        "matching_programs": [_json_safe(row) for row in matching_programs],
        "latest_applied_migration": _json_safe(latest_applied_migration),
    }


def _latest_applied_migration(cur) -> dict[str, Any] | None:
    cur.execute("SELECT to_regclass('public.schema_migrations')")
    ledger_row = cur.fetchone()
    if ledger_row is None or ledger_row[0] is None:
        return None

    cur.execute(_LATEST_APPLIED_MIGRATION_SQL)
    row = cur.fetchone()
    if row is None:
        return None
    application_order, migration_name, checksum_sha256, applied_at = row
    return {
        "application_order": int(application_order),
        "migration_name": str(migration_name),
        "checksum_sha256": str(checksum_sha256),
        "applied_at": _json_safe(applied_at),
    }


def _hash_database_query(conn, name: str, query: str) -> dict[str, Any]:
    digest = hashlib.sha256(_DB_ROW_HASH_PREFIX)
    row_count = 0
    with conn.cursor(name=f"telemetry_hash_{name}") as cur:
        cur.execute(query)
        while rows := cur.fetchmany(_HASH_FETCH_ROWS):
            for row in rows:
                encoded = str(row[0]).encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                row_count += 1
    return {
        "algorithm": "sha256-db-jsonb-rows-v1",
        "sha256": digest.hexdigest(),
        "row_count": row_count,
    }


def _claim_daily_attempt(attempt_date: date) -> bool:
    conn = connect(connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO telemetry_daily_attempt (attempt_date, status)
                VALUES (%s, 'started')
                ON CONFLICT (attempt_date) DO NOTHING
                RETURNING attempt_date
                """,
                (attempt_date,),
            )
            claimed = cur.fetchone() is not None
        conn.commit()
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _record_daily_result(
    attempt_date: date,
    *,
    status: str,
    http_status: int | None,
    error_class: str | None,
) -> bool:
    try:
        conn = connect(connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE telemetry_daily_attempt
                    SET status = %s,
                        completed_at = now(),
                        payload_sha256 = %s,
                        payload_bytes = %s,
                        http_status = %s,
                        error_class = %s
                    WHERE attempt_date = %s
                    RETURNING attempt_date
                    """,
                    (
                        status,
                        None,
                        None,
                        http_status,
                        error_class,
                        attempt_date,
                    ),
                )
                if cur.fetchone() is None:
                    raise RuntimeError(
                        "Daily telemetry attempt row was not found while recording result"
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception:
        logger.exception("Could not record daily telemetry result")
        return False
    return True


def _record_daily_sync_result(
    attempt_date: date,
    result: SyncDeliveryResult,
) -> bool:
    try:
        conn = connect(connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE telemetry_daily_attempt
                    SET status = 'sent',
                        completed_at = now(),
                        payload_sha256 = %s,
                        payload_bytes = %s,
                        http_status = 200,
                        error_class = NULL,
                        sync_id = %s,
                        page_count = %s,
                        pages_sent = %s
                    WHERE attempt_date = %s
                    RETURNING attempt_date
                    """,
                    (
                        result.operation_sha256,
                        result.total_compressed_bytes,
                        result.sync_id,
                        result.page_count,
                        result.page_count,
                        attempt_date,
                    ),
                )
                if cur.fetchone() is None:
                    raise RuntimeError(
                        "Daily telemetry attempt row was not found while recording sync"
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception:
        logger.exception("Could not record daily telemetry sync result")
        return False
    return True
