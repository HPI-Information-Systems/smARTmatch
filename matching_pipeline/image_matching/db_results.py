"""Write final image-matching results back to Postgres."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from matching_pipeline.image_matching.config import (
    MATCHING_PROGRAM_DESCRIPTION,
    MATCHING_PROGRAM_NAME,
    MATCHING_PROGRAM_VERSION,
)
from matching_pipeline.image_matching.result_rows import (
    ImageMatchScoreWrite,
    coerce_image_file_ids,
    prepare_match_score_image_writes,
)
from matching_pipeline.image_matching.results import AcceptedImageMatch, ImageMatchingRunResult
from matching_pipeline.shared.db import connect_db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DbWriteResult:
    matching_program_id: UUID | None
    accepted_image_match_count: int
    match_score_count: int
    processed_auction_file_count: int
    processed_auction_link_count: int
    processed_auction_artwork_count: int
    empty_auction_artwork_count: int


def write_matching_run_to_db(result: ImageMatchingRunResult) -> DbWriteResult:
    return write_image_matching_results_to_db(
        result.accepted_matches,
        result.processed_auction_file_ids,
    )


def write_image_matching_results_to_db(
    accepted_matches: Sequence[AcceptedImageMatch],
    processed_auction_file_ids: Sequence[str],
) -> DbWriteResult:
    """Persist accepted matches and mark processed auction images atomically."""
    processed_ids = coerce_image_file_ids(processed_auction_file_ids, "auction_file_id")
    accepted_auction_ids = coerce_image_file_ids(
        [match.auction_file_id for match in accepted_matches], "auction_file_id"
    )
    lost_ids = coerce_image_file_ids(
        [match.lost_file_id for match in accepted_matches], "lost_file_id"
    )
    auction_ids_to_resolve = _merge_ids(processed_ids, accepted_auction_ids)
    program_id = (
        stable_matching_program_id(MATCHING_PROGRAM_NAME, MATCHING_PROGRAM_VERSION)
        if accepted_matches
        else None
    )

    conn = connect_db()
    try:
        with conn.cursor() as cur:
            writes: list[ImageMatchScoreWrite] = []
            if accepted_matches:
                assert program_id is not None
                _ensure_matching_program(cur, program_id)
                auction_links = _fetch_links(
                    cur,
                    "auction_artwork_image_file",
                    "auction_artwork_id",
                    auction_ids_to_resolve,
                )
                _require_links(auction_links, accepted_auction_ids, "auction")
                lost_links = _fetch_links(
                    cur, "lost_artwork_image_file", "lost_artwork_id", lost_ids
                )
                _require_links(lost_links, lost_ids, "lost")
                writes = prepare_match_score_image_writes(
                    accepted_matches,
                    auction_links=auction_links,
                    lost_links=lost_links,
                    matching_program_id=program_id,
                )
                _upsert_match_scores(cur, program_id, writes)
            finalized = _finalize_processed_auction_links(cur, processed_ids)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info(
        "Image-matching DB write complete: accepted_image_matches=%d "
        "match_scores=%d processed_auction_files=%d processed_links=%d "
        "processed_artworks=%d empty_artworks=%d program_id=%s",
        len(accepted_matches),
        len(writes),
        len(processed_ids),
        finalized.processed_auction_link_count,
        finalized.processed_auction_artwork_count,
        finalized.empty_auction_artwork_count,
        program_id,
    )
    return DbWriteResult(
        program_id,
        len(accepted_matches),
        len(writes),
        len(processed_ids),
        finalized.processed_auction_link_count,
        finalized.processed_auction_artwork_count,
        finalized.empty_auction_artwork_count,
    )


def stable_matching_program_id(name: str, version: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"smartmatch:matching_program:{name}:{version}")


def _ensure_matching_program(cur, program_id: UUID) -> None:
    cur.execute(
        """
        INSERT INTO matching_program (matching_program_id, name, version, description)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (matching_program_id) DO UPDATE SET name = EXCLUDED.name,
            version = EXCLUDED.version, description = EXCLUDED.description
        """,
        (program_id, MATCHING_PROGRAM_NAME, MATCHING_PROGRAM_VERSION, MATCHING_PROGRAM_DESCRIPTION),
    )


def _fetch_links(
    cur, table_name: str, artwork_id_column: str, image_file_ids: Sequence[int]
) -> dict[int, list[UUID]]:
    if not image_file_ids:
        return {}
    if table_name not in {"auction_artwork_image_file", "lost_artwork_image_file"}:
        raise ValueError(f"Unsupported image link table: {table_name}")
    if artwork_id_column not in {"auction_artwork_id", "lost_artwork_id"}:
        raise ValueError(f"Unsupported artwork id column: {artwork_id_column}")
    cur.execute(
        f"""
        SELECT image_file_id, {artwork_id_column}
        FROM {table_name}
        WHERE image_file_id = ANY(%s)
        ORDER BY image_file_id, {artwork_id_column}
        """,
        (list(image_file_ids),),
    )
    grouped: dict[int, list[UUID]] = {}
    for image_file_id, artwork_id in cur.fetchall():
        grouped.setdefault(int(image_file_id), []).append(artwork_id)
    return grouped


def _require_links(links: dict[int, list[UUID]], image_file_ids: Sequence[int], role: str) -> None:
    missing = sorted(set(image_file_ids) - set(links))
    if missing:
        raise ValueError(f"No {role} artwork links found for image_file_id values: {missing}")


def _upsert_match_scores(
    cur, matching_program_id: UUID, writes: Sequence[ImageMatchScoreWrite]
) -> None:
    if not writes:
        return
    cur.executemany(
        """
        INSERT INTO match_score (
            lost_id, auction_id, image_matching_confidence, image_final_score,
            image_blocking_similarity, image_match_date, image_matching_program,
            image_visualization, best_image_file_id)
        VALUES (%s, %s, %s, %s, %s, now(), %s, %s::jsonb, %s)
        ON CONFLICT (lost_id, auction_id) DO UPDATE SET
            image_matching_confidence = EXCLUDED.image_matching_confidence,
            image_final_score = EXCLUDED.image_final_score,
            image_blocking_similarity = EXCLUDED.image_blocking_similarity,
            image_match_date = EXCLUDED.image_match_date,
            image_matching_program = EXCLUDED.image_matching_program,
            image_visualization = EXCLUDED.image_visualization,
            best_image_file_id = EXCLUDED.best_image_file_id
        WHERE COALESCE(EXCLUDED.image_final_score, -2) > COALESCE(match_score.image_final_score, -2)
           OR (
               COALESCE(EXCLUDED.image_final_score, -2) = COALESCE(match_score.image_final_score, -2)
               AND COALESCE(EXCLUDED.image_matching_confidence, -1) > COALESCE(match_score.image_matching_confidence, -1)
           )
           OR (
               COALESCE(EXCLUDED.image_final_score, -2) = COALESCE(match_score.image_final_score, -2)
               AND COALESCE(EXCLUDED.image_matching_confidence, -1) = COALESCE(match_score.image_matching_confidence, -1)
               AND COALESCE(EXCLUDED.image_blocking_similarity, -2) >= COALESCE(match_score.image_blocking_similarity, -2)
           )
        """,
        [
            (
                row.lost_artwork_id,
                row.auction_artwork_id,
                row.image_matching_confidence,
                row.image_final_score,
                row.image_blocking_similarity,
                matching_program_id,
                json.dumps(row.image_visualization, sort_keys=True),
                row.best_image_file_id,
            )
            for row in writes
        ],
    )
    _invalidate_metadata_matching_for_image_only_pairs(cur, writes)


def _invalidate_metadata_matching_for_image_only_pairs(
    cur, writes: Sequence[ImageMatchScoreWrite]
) -> None:
    pairs = list(
        dict.fromkeys(
            (row.lost_artwork_id, row.auction_artwork_id) for row in writes
        )
    )
    if not pairs:
        return
    cur.execute(
        """
        WITH written_pairs AS (
            SELECT pair.lost_id, pair.auction_id
            FROM unnest(%s::uuid[], %s::uuid[]) AS pair(lost_id, auction_id)
        ), affected_auctions AS (
            SELECT DISTINCT score.auction_id
            FROM written_pairs pair
            JOIN match_score score
              ON score.lost_id = pair.lost_id
             AND score.auction_id = pair.auction_id
            WHERE score.metadata_final_score IS NULL
              AND score.image_final_score IS NOT NULL
        )
        UPDATE auction_artwork artwork
        SET is_metadata_matching_processed = false,
            is_metadata_matching_processed_at = NULL
        FROM affected_auctions affected
        WHERE artwork.auction_artwork_id = affected.auction_id
          AND (
              artwork.is_metadata_matching_processed = true
              OR artwork.is_metadata_matching_processed_at IS NOT NULL
          )
        """,
        (
            [lost_id for lost_id, _auction_id in pairs],
            [auction_id for _lost_id, auction_id in pairs],
        ),
    )


@dataclass(frozen=True)
class _FinalizedAuctionLinks:
    processed_auction_link_count: int
    processed_auction_artwork_count: int
    empty_auction_artwork_count: int


def _finalize_processed_auction_links(
    cur, image_file_ids: Sequence[int]
) -> _FinalizedAuctionLinks:
    cur.execute(
        """
        WITH input_ids AS (
            SELECT unnest(%s::int[]) AS image_file_id
        ), processed_links AS (
            UPDATE auction_artwork_image_file aaif
            SET is_image_matching_processed = true,
                is_image_matching_completed_without_error = true
            FROM input_ids
            WHERE aaif.image_file_id = input_ids.image_file_id
              AND (
                  aaif.is_image_matching_processed = false
                  OR aaif.is_image_matching_completed_without_error = false
              )
            RETURNING aaif.auction_artwork_id, aaif.image_file_id
        ), completed_artworks AS (
            UPDATE auction_artwork aa
            SET is_image_matching_processed = true
            WHERE aa.is_image_matching_processed = false
              AND (
                  NOT EXISTS (
                      SELECT 1
                      FROM auction_artwork_image_file any_link
                      WHERE any_link.auction_artwork_id = aa.auction_artwork_id
                  )
                  OR NOT EXISTS (
                      SELECT 1
                      FROM auction_artwork_image_file pending
                      WHERE pending.auction_artwork_id = aa.auction_artwork_id
                        AND (
                            pending.is_image_matching_processed = false
                            OR pending.is_image_matching_completed_without_error = false
                        )
                        AND NOT EXISTS (
                            SELECT 1
                            FROM input_ids
                            WHERE input_ids.image_file_id = pending.image_file_id
                        )
                  )
              )
            RETURNING aa.auction_artwork_id
        ), completed_empty_artworks AS (
            SELECT completed_artworks.auction_artwork_id
            FROM completed_artworks
            WHERE NOT EXISTS (
                SELECT 1
                FROM auction_artwork_image_file link
                WHERE link.auction_artwork_id = completed_artworks.auction_artwork_id
            )
        )
        SELECT
            (SELECT count(*) FROM processed_links),
            (SELECT count(*) FROM completed_artworks),
            (SELECT count(*) FROM completed_empty_artworks)
        """,
        (list(image_file_ids),),
    )
    row = cur.fetchone()
    return _FinalizedAuctionLinks(*(int(value or 0) for value in row))


def _merge_ids(*id_lists: Sequence[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for ids in id_lists:
        for image_file_id in ids:
            if image_file_id not in seen:
                seen.add(image_file_id)
                result.append(image_file_id)
    return result
