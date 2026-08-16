"""Three-step, paginated database replication over telemetry HTTP events."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from matching_pipeline.shared.db import connect

logger = logging.getLogger(__name__)

SYNC_SCHEMA_VERSION = 3
INVENTORY_MATCHES_PER_PAGE = 500
DATA_MATCHES_PER_PAGE = 25
MAX_COMPRESSED_PAGE_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_PAGE_BYTES = 20 * 1024 * 1024
TARGET_UNCOMPRESSED_PAGE_BYTES = 4 * 1024 * 1024
PAGE_RETRIES = 3


class SyncSettings(Protocol):
    endpoint: str
    timeout_seconds: float
    auth_token: str


@dataclass(frozen=True)
class RawPage:
    path: Path
    content_sha256: str
    counts: Mapping[str, int]


@dataclass(frozen=True)
class EncodedSyncPage:
    body: bytes
    uncompressed_sha256: str
    uncompressed_bytes: int


@dataclass(frozen=True)
class SyncDeliveryResult:
    sync_id: str
    page_count: int
    total_compressed_bytes: int
    operation_sha256: str
    last_page: EncodedSyncPage


class SyncHttpError(RuntimeError):
    def __init__(self, status: int, message: str | None = None) -> None:
        detail = f": {message}" if message else ""
        super().__init__(f"Telemetry sync endpoint returned HTTP {status}{detail}")
        self.status = status


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def deliver_sync_operation(
    settings: SyncSettings,
    *,
    trigger: str,
    generated_at: datetime,
    summary: Mapping[str, Any],
) -> SyncDeliveryResult:
    """Run inventory, receiver-selection, and requested-data phases for one sync."""
    generated_at = _as_utc(generated_at)
    sync_id = str(uuid4())
    started_at = time.monotonic()
    total_compressed_bytes = 0
    requested_matches: set[tuple[str, str]] = set()
    requested_lost: set[str] = set()
    requested_auction: set[str] = set()
    advertised_inventory: dict[str, dict[str, Any]] = {
        "match_score": {},
        "lost_artwork": {},
        "auction_artwork": {},
    }
    logger.info(
        "Telemetry sync started sync_id=%s trigger=%s generated_at=%s",
        sync_id,
        trigger,
        _isoformat(generated_at),
    )

    with tempfile.TemporaryDirectory(prefix="smartmatch-sync-") as temp_dir:
        root = Path(temp_dir)
        logger.info("Telemetry sync inventory spooling started sync_id=%s", sync_id)
        snapshot_conn = _snapshot_connection()
        try:
            inventory_pages = _spool_inventory_pages(
                root / "inventory", conn=snapshot_conn
            )
            logger.info(
                "Telemetry sync inventory spooling complete sync_id=%s pages=%d matches=%d",
                sync_id,
                len(inventory_pages),
                sum(page.counts.get("match_score", 0) for page in inventory_pages),
            )
            for page_number, raw_page in enumerate(inventory_pages):
                envelope = _page_envelope(
                    sync_id=sync_id,
                    trigger=trigger,
                    generated_at=generated_at,
                    phase="inventory",
                    page_number=page_number,
                    pages=inventory_pages,
                    content=json.loads(raw_page.path.read_text(encoding="utf-8")),
                    summary=(dict(summary) if page_number == 0 else None),
                )
                encoded = encode_sync_page(envelope)
                logger.info(
                    "Telemetry sync progress sync_id=%s phase=inventory page=%d/%d "
                    "status=sending compressed_bytes=%d",
                    sync_id,
                    page_number + 1,
                    len(inventory_pages),
                    len(encoded.body),
                )
                acknowledgement = _post_page_with_retries(
                    settings,
                    encoded,
                    sync_id=sync_id,
                    phase="inventory",
                    page_number=page_number,
                    page_count=len(inventory_pages),
                )
                page_inventory = envelope["inventory"]
                for entity_type in advertised_inventory:
                    advertised_inventory[entity_type].update(
                        page_inventory[entity_type]
                    )
                needed = acknowledgement.get("needed") or {}
                allowed_inventory = (
                    advertised_inventory
                    if page_number == len(inventory_pages) - 1
                    else page_inventory
                )
                _validate_needed_acknowledgement(needed, allowed_inventory)
                requested_matches.update(
                    (str(row["lost_id"]), str(row["auction_id"]))
                    for row in (needed.get("match_score") or [])
                )
                requested_lost.update(
                    str(value) for value in needed.get("lost_artwork") or []
                )
                requested_auction.update(
                    str(value) for value in needed.get("auction_artwork") or []
                )
                total_compressed_bytes += len(encoded.body)
                logger.info(
                    "Telemetry sync progress sync_id=%s phase=inventory page=%d/%d "
                    "status=acknowledged requested_matches=%d requested_lost=%d "
                    "requested_auction=%d",
                    sync_id,
                    page_number + 1,
                    len(inventory_pages),
                    len(requested_matches),
                    len(requested_lost),
                    len(requested_auction),
                )

            # A requested match is always shipped with both full artwork entities.
            for lost_id, auction_id in requested_matches:
                requested_lost.add(lost_id)
                requested_auction.add(auction_id)

            logger.info(
                "Telemetry sync data spooling started sync_id=%s matches=%d lost=%d "
                "auction=%d",
                sync_id,
                len(requested_matches),
                len(requested_lost),
                len(requested_auction),
            )
            data_pages = _spool_data_pages(
                root / "data",
                requested_matches=requested_matches,
                requested_lost=requested_lost,
                requested_auction=requested_auction,
                conn=snapshot_conn,
            )
            logger.info(
                "Telemetry sync data spooling complete sync_id=%s pages=%d",
                sync_id,
                len(data_pages),
            )
            snapshot_conn.rollback()
        finally:
            snapshot_conn.close()

        operation_hash = _operation_hash(data_pages)
        last_encoded: EncodedSyncPage | None = None
        for page_number, raw_page in enumerate(data_pages):
            envelope = _page_envelope(
                sync_id=sync_id,
                trigger=trigger,
                generated_at=generated_at,
                phase="data",
                page_number=page_number,
                pages=data_pages,
                content=json.loads(raw_page.path.read_text(encoding="utf-8")),
                summary=None,
                operation_hash=operation_hash,
            )
            encoded = encode_sync_page(envelope)
            logger.info(
                "Telemetry sync progress sync_id=%s phase=data page=%d/%d "
                "status=sending compressed_bytes=%d",
                sync_id,
                page_number + 1,
                len(data_pages),
                len(encoded.body),
            )
            _post_page_with_retries(
                settings,
                encoded,
                sync_id=sync_id,
                phase="data",
                page_number=page_number,
                page_count=len(data_pages),
            )
            total_compressed_bytes += len(encoded.body)
            last_encoded = encoded
            logger.info(
                "Telemetry sync progress sync_id=%s phase=data page=%d/%d "
                "status=acknowledged",
                sync_id,
                page_number + 1,
                len(data_pages),
            )

    assert last_encoded is not None
    result = SyncDeliveryResult(
        sync_id=sync_id,
        page_count=len(inventory_pages) + len(data_pages),
        total_compressed_bytes=total_compressed_bytes,
        operation_sha256=operation_hash,
        last_page=last_encoded,
    )
    logger.info(
        "Telemetry sync complete sync_id=%s trigger=%s pages=%d compressed_bytes=%d "
        "elapsed_seconds=%.1f",
        sync_id,
        trigger,
        result.page_count,
        result.total_compressed_bytes,
        time.monotonic() - started_at,
    )
    return result


def _page_envelope(
    *,
    sync_id: str,
    trigger: str,
    generated_at: datetime,
    phase: str,
    page_number: int,
    pages: Sequence[RawPage],
    content: Mapping[str, Any],
    summary: Mapping[str, Any] | None,
    operation_hash: str | None = None,
) -> dict[str, Any]:
    final = page_number == len(pages) - 1
    raw_page = pages[page_number]
    return {
        "schema_version": SYNC_SCHEMA_VERSION,
        "operation": {
            "sync_id": sync_id,
            "trigger": trigger,
            "generated_at": _isoformat(generated_at),
            "selection": "matched_artworks_only",
        },
        "page": {
            "phase": phase,
            "number": page_number,
            "page_count": len(pages),
            "final": final,
            "content_sha256": raw_page.content_sha256,
            "previous_content_sha256": (
                pages[page_number - 1].content_sha256 if page_number else None
            ),
            "record_counts": dict(raw_page.counts),
        },
        **dict(content),
        "summary": summary,
        "manifest": (
            {
                "complete": True,
                "truncated": False,
                "phase": phase,
                "page_count": len(pages),
                "operation_sha256": operation_hash or _operation_hash(pages),
                "ordered_page_content_sha256": [page.content_sha256 for page in pages],
            }
            if final
            else None
        ),
    }


def encode_sync_page(envelope: Mapping[str, Any]) -> EncodedSyncPage:
    raw = _canonical_json(envelope)
    if len(raw) > MAX_UNCOMPRESSED_PAGE_BYTES:
        raise ValueError(
            f"Telemetry sync page is {len(raw)} bytes; maximum is "
            f"{MAX_UNCOMPRESSED_PAGE_BYTES}"
        )
    body = gzip.compress(raw, compresslevel=6, mtime=0)
    if len(body) > MAX_COMPRESSED_PAGE_BYTES:
        raise ValueError(
            f"Compressed telemetry sync page is {len(body)} bytes; maximum is "
            f"{MAX_COMPRESSED_PAGE_BYTES}"
        )
    return EncodedSyncPage(
        body=body,
        uncompressed_sha256=hashlib.sha256(raw).hexdigest(),
        uncompressed_bytes=len(raw),
    )


def _spool_inventory_pages(directory: Path, *, conn=None) -> list[RawPage]:
    directory.mkdir(parents=True, exist_ok=True)
    pages: list[RawPage] = []
    owns_connection = conn is None
    conn = conn or _snapshot_connection()
    try:
        cursor: tuple[str, str] | None = None
        matches_spooled = 0
        while True:
            rows = _fetch_inventory_rows(conn, cursor, INVENTORY_MATCHES_PER_PAGE)
            if not rows:
                break
            match_rows = [row["match_score"] for row in rows]
            data_content = _build_data_content(conn, match_rows, set(), set())
            graph_hashes = data_content["hashes"]
            matches: dict[str, dict[str, Any]] = {}
            lost: dict[str, str] = {}
            auction: dict[str, str] = {}
            for row in rows:
                lost_id = str(row["lost_id"])
                auction_id = str(row["auction_id"])
                key = _match_key(lost_id, auction_id)
                matches[key] = {
                    "lost_id": lost_id,
                    "auction_id": auction_id,
                    "sha256": graph_hashes["match_score"][key],
                }
                lost[lost_id] = graph_hashes["lost_artwork"][lost_id]
                auction[auction_id] = graph_hashes["auction_artwork"][auction_id]
            content = {
                "inventory": {
                    "match_score": matches,
                    "lost_artwork": lost,
                    "auction_artwork": auction,
                }
            }
            _write_raw_page(directory, content, pages, _inventory_counts(content))
            matches_spooled += len(rows)
            logger.info(
                "Telemetry inventory spool progress pages=%d matches=%d",
                len(pages),
                matches_spooled,
            )
            last = rows[-1]
            cursor = (str(last["lost_id"]), str(last["auction_id"]))
        if not pages:
            content = {
                "inventory": {
                    "match_score": {},
                    "lost_artwork": {},
                    "auction_artwork": {},
                }
            }
            _write_raw_page(directory, content, pages, _inventory_counts(content))
        if owns_connection:
            conn.rollback()
    finally:
        if owns_connection:
            conn.close()
    return pages


def _fetch_inventory_rows(
    conn,
    cursor: tuple[str, str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    params: tuple[Any, ...]
    where = ""
    if cursor is None:
        params = (limit,)
    else:
        where = "WHERE (ms.lost_id, ms.auction_id) > (%s::uuid, %s::uuid)"
        params = (cursor[0], cursor[1], limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                ms.lost_id,
                ms.auction_id,
                to_jsonb(ms) AS match_score,
                to_jsonb(lost) AS lost_artwork,
                to_jsonb(auction) AS auction_artwork
            FROM match_score ms
            JOIN lost_artwork lost ON lost.lost_artwork_id = ms.lost_id
            JOIN auction_artwork auction ON auction.auction_artwork_id = ms.auction_id
            {where}
            ORDER BY ms.lost_id, ms.auction_id
            LIMIT %s
            """,
            params,
        )
        return [
            {
                "lost_id": row[0],
                "auction_id": row[1],
                "match_score": dict(row[2]),
                "lost_artwork": dict(row[3]),
                "auction_artwork": dict(row[4]),
            }
            for row in cur.fetchall()
        ]


def _spool_data_pages(
    directory: Path,
    *,
    requested_matches: set[tuple[str, str]],
    requested_lost: set[str],
    requested_auction: set[str],
    conn=None,
) -> list[RawPage]:
    directory.mkdir(parents=True, exist_ok=True)
    pages: list[RawPage] = []
    owns_connection = conn is None
    conn = conn or _snapshot_connection()
    try:
        match_pairs = sorted(requested_matches)
        matched_lost: set[str] = set()
        matched_auction: set[str] = set()
        for offset in range(0, len(match_pairs), DATA_MATCHES_PER_PAGE):
            batch = match_pairs[offset : offset + DATA_MATCHES_PER_PAGE]
            rows = _fetch_requested_match_rows(conn, batch)
            matched_lost.update(str(row["lost_id"]) for row in rows)
            matched_auction.update(str(row["auction_id"]) for row in rows)
            _spool_data_content(conn, directory, rows, set(), set(), pages)
            logger.info(
                "Telemetry data spool progress matches=%d/%d pages=%d",
                min(offset + len(batch), len(match_pairs)),
                len(match_pairs),
                len(pages),
            )

        extra_lost = sorted(requested_lost - matched_lost)
        extra_auction = sorted(requested_auction - matched_auction)
        extra_lost_total = len(extra_lost)
        extra_auction_total = len(extra_auction)
        extra_lost_spooled = 0
        extra_auction_spooled = 0
        while extra_lost or extra_auction:
            lost_batch = set(extra_lost[:DATA_MATCHES_PER_PAGE])
            auction_batch = set(extra_auction[:DATA_MATCHES_PER_PAGE])
            del extra_lost[:DATA_MATCHES_PER_PAGE]
            del extra_auction[:DATA_MATCHES_PER_PAGE]
            _spool_data_content(
                conn,
                directory,
                [],
                lost_batch,
                auction_batch,
                pages,
            )
            extra_lost_spooled += len(lost_batch)
            extra_auction_spooled += len(auction_batch)
            logger.info(
                "Telemetry data spool progress extra_lost=%d/%d "
                "extra_auction=%d/%d pages=%d",
                extra_lost_spooled,
                extra_lost_total,
                extra_auction_spooled,
                extra_auction_total,
                len(pages),
            )

        if not pages:
            _write_raw_page(
                directory,
                _build_data_content(conn, [], set(), set()),
                pages,
                {"match_score": 0, "lost_artwork": 0, "auction_artwork": 0},
            )
        if owns_connection:
            conn.rollback()
    finally:
        if owns_connection:
            conn.close()
    return pages


def _spool_data_content(
    conn,
    directory: Path,
    match_rows: Sequence[Mapping[str, Any]],
    lost_ids: set[str],
    auction_ids: set[str],
    pages: list[RawPage],
) -> None:
    content = _build_data_content(conn, match_rows, lost_ids, auction_ids)
    raw = _canonical_json(content)
    item_count = len(match_rows) + len(lost_ids) + len(auction_ids)
    if len(raw) > TARGET_UNCOMPRESSED_PAGE_BYTES and item_count > 1:
        if len(match_rows) > 1:
            middle = len(match_rows) // 2
            _spool_data_content(
                conn, directory, match_rows[:middle], set(), set(), pages
            )
            _spool_data_content(
                conn, directory, match_rows[middle:], set(), set(), pages
            )
        else:
            lost_values = sorted(lost_ids)
            auction_values = sorted(auction_ids)
            combined = [("lost", value) for value in lost_values] + [
                ("auction", value) for value in auction_values
            ]
            middle = len(combined) // 2
            for half in (combined[:middle], combined[middle:]):
                _spool_data_content(
                    conn,
                    directory,
                    [],
                    {value for kind, value in half if kind == "lost"},
                    {value for kind, value in half if kind == "auction"},
                    pages,
                )
        return
    if len(raw) > MAX_UNCOMPRESSED_PAGE_BYTES:
        raise ValueError("A requested match/entity closure exceeds the page limit")
    counts = {
        "match_score": len(content["rows"]["match_score"]),
        "lost_artwork": len(content["entities"]["lost_artwork"]),
        "auction_artwork": len(content["entities"]["auction_artwork"]),
    }
    _write_raw_page(directory, content, pages, counts, raw=raw)


def _fetch_requested_match_rows(
    conn,
    pairs: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    if not pairs:
        return []
    keys = [_match_key(lost_id, auction_id) for lost_id, auction_id in pairs]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT to_jsonb(ms)
            FROM match_score ms
            WHERE ms.lost_id::text || ':' || ms.auction_id::text = ANY(%s)
            ORDER BY ms.lost_id, ms.auction_id
            """,
            (keys,),
        )
        return [dict(row[0]) for row in cur.fetchall()]


def _build_data_content(
    conn,
    match_rows: Sequence[Mapping[str, Any]],
    extra_lost_ids: set[str],
    extra_auction_ids: set[str],
) -> dict[str, Any]:
    lost_ids = _values(match_rows, "lost_id") | set(extra_lost_ids)
    auction_ids = _values(match_rows, "auction_id") | set(extra_auction_ids)
    lost = _fetch_entities(conn, "lost_artwork", "lost_artwork_id", lost_ids)
    auction = _fetch_entities(
        conn, "auction_artwork", "auction_artwork_id", auction_ids
    )

    artist_ids = set()
    for row in lost.values():
        artist_ids.update(str(value) for value in (row.get("artist_ids") or []))
    artist_ids.update(_values(auction.values(), "artist_id"))
    artists = _fetch_entities(conn, "artist", "artist_id", artist_ids)
    institutions = _fetch_entities(
        conn,
        "institution",
        "institution_id",
        _values(lost.values(), "institution_id"),
    )
    literature = _fetch_entities(
        conn,
        "literature_source",
        "literature_id",
        _values(lost.values(), "literature_source_id"),
    )
    platforms = _fetch_entities(
        conn,
        "auction_platform",
        "auction_platform_id",
        _values(auction.values(), "auction_platform_id"),
    )
    auctioneers = _fetch_entities(
        conn,
        "auctioneer",
        "auctioneer_id",
        _values(auction.values(), "auctioneer_id"),
    )
    experts = _fetch_entities(
        conn,
        "expert",
        "expert_id",
        _values(auction.values(), "expert_id"),
    )
    program_ids = _values(match_rows, "metadata_matching_program") | _values(
        match_rows, "image_matching_program"
    )
    programs = _fetch_entities(
        conn, "matching_program", "matching_program_id", program_ids
    )
    location_ids = (
        _values(auction.values(), "artist_birth_place")
        | _values(auction.values(), "artist_death_place")
        | _values(artists.values(), "place_of_birth")
        | _values(artists.values(), "place_of_death")
        | _values(literature.values(), "publishing_location_id")
    )
    locations = _fetch_entities(conn, "location", "location_id", location_ids)

    lost_links = _fetch_link_rows(
        conn, "lost_artwork_image_file", "lost_artwork_id", lost_ids
    )
    auction_links = _fetch_link_rows(
        conn, "auction_artwork_image_file", "auction_artwork_id", auction_ids
    )
    image_ids = {
        int(row["image_file_id"])
        for row in (*lost_links, *auction_links)
        if row.get("image_file_id") is not None
    }
    image_ids.update(
        int(value)
        for value in (row.get("best_image_file_id") for row in match_rows)
        if value is not None
    )
    image_files = _fetch_integer_entities(conn, image_ids)

    match_dict = {
        _match_key(str(row["lost_id"]), str(row["auction_id"])): dict(row)
        for row in match_rows
    }
    content = {
        "entities": {
            "location": locations,
            "artist": artists,
            "institution": institutions,
            "literature_source": literature,
            "auction_platform": platforms,
            "auctioneer": auctioneers,
            "expert": experts,
            "matching_program": programs,
            "image_file": image_files,
            "lost_artwork": lost,
            "auction_artwork": auction,
        },
        "rows": {
            "lost_artwork_image_file": lost_links,
            "auction_artwork_image_file": auction_links,
            "match_score": list(match_dict.values()),
        },
        "hashes": {},
    }
    content["hashes"] = _replication_graph_hashes(content)
    return content


def _replication_graph_hashes(content: Mapping[str, Any]) -> dict[str, Any]:
    entities = content["entities"]
    rows = content["rows"]
    lost_links = rows.get("lost_artwork_image_file") or []
    auction_links = rows.get("auction_artwork_image_file") or []

    def selected(entity_type: str, identifiers: Sequence[Any]) -> dict[str, Any]:
        collection = entities.get(entity_type) or {}
        keys = sorted({str(value) for value in identifiers if value is not None})
        return {key: collection[key] for key in keys if key in collection}

    def images_for(link_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return selected(
            "image_file",
            [row.get("image_file_id") for row in link_rows],
        )

    lost_hashes: dict[str, str] = {}
    for entity_id, artwork in (entities.get("lost_artwork") or {}).items():
        artwork_links = [
            row for row in lost_links if str(row.get("lost_artwork_id")) == entity_id
        ]
        artists = selected("artist", artwork.get("artist_ids") or [])
        literature = selected(
            "literature_source", [artwork.get("literature_source_id")]
        )
        locations = selected(
            "location",
            [
                value
                for artist in artists.values()
                for value in (
                    artist.get("place_of_birth"),
                    artist.get("place_of_death"),
                )
            ]
            + [row.get("publishing_location_id") for row in literature.values()],
        )
        lost_hashes[entity_id] = _canonical_hash(
            {
                "artwork": artwork,
                "artists": artists,
                "institutions": selected(
                    "institution", [artwork.get("institution_id")]
                ),
                "literature_sources": literature,
                "locations": locations,
                "image_links": artwork_links,
                "image_files": images_for(artwork_links),
            }
        )

    auction_hashes: dict[str, str] = {}
    for entity_id, artwork in (entities.get("auction_artwork") or {}).items():
        artwork_links = [
            row
            for row in auction_links
            if str(row.get("auction_artwork_id")) == entity_id
        ]
        artists = selected("artist", [artwork.get("artist_id")])
        locations = selected(
            "location",
            [artwork.get("artist_birth_place"), artwork.get("artist_death_place")]
            + [
                value
                for artist in artists.values()
                for value in (
                    artist.get("place_of_birth"),
                    artist.get("place_of_death"),
                )
            ],
        )
        auction_hashes[entity_id] = _canonical_hash(
            {
                "artwork": artwork,
                "artists": artists,
                "locations": locations,
                "auction_platforms": selected(
                    "auction_platform", [artwork.get("auction_platform_id")]
                ),
                "auctioneers": selected("auctioneer", [artwork.get("auctioneer_id")]),
                "experts": selected("expert", [artwork.get("expert_id")]),
                "image_links": artwork_links,
                "image_files": images_for(artwork_links),
            }
        )

    match_hashes: dict[str, str] = {}
    for match in rows.get("match_score") or []:
        key = _match_key(str(match["lost_id"]), str(match["auction_id"]))
        match_hashes[key] = _canonical_hash(
            {
                "match_score": match,
                "matching_programs": selected(
                    "matching_program",
                    [
                        match.get("metadata_matching_program"),
                        match.get("image_matching_program"),
                    ],
                ),
                "best_image_file": selected(
                    "image_file", [match.get("best_image_file_id")]
                ),
            }
        )
    return {
        "match_score": match_hashes,
        "lost_artwork": lost_hashes,
        "auction_artwork": auction_hashes,
    }


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
    values = sorted({str(value) for value in ids if value is not None})
    if not values:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {primary_key}::text, to_jsonb(row_data)
            FROM {table} row_data
            WHERE {primary_key}::text = ANY(%s)
            ORDER BY {primary_key}
            """,
            (values,),
        )
        return {str(key): dict(row) for key, row in cur.fetchall()}


def _fetch_integer_entities(conn, ids: set[int]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT image_file_id::text, to_jsonb(row_data)
            FROM image_file row_data
            WHERE image_file_id = ANY(%s)
            ORDER BY image_file_id
            """,
            (sorted(ids),),
        )
        return {str(key): dict(row) for key, row in cur.fetchall()}


def _fetch_link_rows(
    conn,
    table: str,
    artwork_id_column: str,
    artwork_ids: set[str],
) -> list[dict[str, Any]]:
    allowed = {
        ("lost_artwork_image_file", "lost_artwork_id"),
        ("auction_artwork_image_file", "auction_artwork_id"),
    }
    if (table, artwork_id_column) not in allowed:
        raise ValueError(f"Unsupported telemetry link table: {table}")
    if not artwork_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT to_jsonb(row_data)
            FROM {table} row_data
            WHERE {artwork_id_column}::text = ANY(%s)
            ORDER BY {artwork_id_column}, image_file_id
            """,
            (sorted(artwork_ids),),
        )
        return [dict(row[0]) for row in cur.fetchall()]


def _write_raw_page(
    directory: Path,
    content: Mapping[str, Any],
    pages: list[RawPage],
    counts: Mapping[str, int],
    *,
    raw: bytes | None = None,
) -> None:
    raw = raw if raw is not None else _canonical_json(content)
    path = directory / f"page-{len(pages):08d}.json"
    path.write_bytes(raw)
    pages.append(
        RawPage(
            path=path,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            counts=dict(counts),
        )
    )


def _inventory_counts(content: Mapping[str, Any]) -> dict[str, int]:
    inventory = content["inventory"]
    return {key: len(value) for key, value in inventory.items()}


def _validate_needed_acknowledgement(
    needed: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> None:
    if not isinstance(needed, Mapping):
        raise ValueError("Telemetry acknowledgement needed field must be an object")
    allowed_fields = {"match_score", "lost_artwork", "auction_artwork"}
    if set(needed) - allowed_fields:
        raise ValueError("Telemetry acknowledgement contains unknown needed fields")
    allowed_matches = set((inventory.get("match_score") or {}).keys())
    allowed_lost = set((inventory.get("lost_artwork") or {}).keys())
    allowed_auction = set((inventory.get("auction_artwork") or {}).keys())
    match_items = needed.get("match_score") or []
    if not isinstance(match_items, list) or any(
        not isinstance(row, Mapping) or "lost_id" not in row or "auction_id" not in row
        for row in match_items
    ):
        raise ValueError("Telemetry acknowledgement has invalid match IDs")
    requested_matches = {
        _match_key(str(row["lost_id"]), str(row["auction_id"])) for row in match_items
    }
    lost_items = needed.get("lost_artwork") or []
    auction_items = needed.get("auction_artwork") or []
    if not isinstance(lost_items, list) or not isinstance(auction_items, list):
        raise ValueError("Telemetry acknowledgement artwork IDs must be arrays")
    requested_lost = {str(value) for value in lost_items}
    requested_auction = {str(value) for value in auction_items}
    if requested_matches - allowed_matches:
        raise ValueError(
            "Receiver requested match IDs outside the advertised inventory"
        )
    if requested_lost - allowed_lost:
        raise ValueError("Receiver requested lost-artwork IDs outside the inventory")
    if requested_auction - allowed_auction:
        raise ValueError("Receiver requested auction-artwork IDs outside the inventory")


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


def _operation_hash(pages: Sequence[RawPage]) -> str:
    digest = hashlib.sha256(b"smartmatch-sync-operation-v3\0")
    for page in pages:
        digest.update(page.content_sha256.encode("ascii"))
    return digest.hexdigest()


def _post_page_with_retries(
    settings: SyncSettings,
    encoded: EncodedSyncPage,
    *,
    sync_id: str,
    phase: str,
    page_number: int,
    page_count: int,
) -> dict[str, Any]:
    for attempt in range(1, PAGE_RETRIES + 1):
        try:
            return _post_page(
                settings,
                encoded,
                sync_id=sync_id,
                phase=phase,
                page_number=page_number,
                page_count=page_count,
            )
        except Exception as exc:
            if attempt == PAGE_RETRIES:
                logger.exception(
                    "Telemetry page delivery failed sync_id=%s phase=%s page=%d/%d "
                    "attempt=%d/%d",
                    sync_id,
                    phase,
                    page_number + 1,
                    page_count,
                    attempt,
                    PAGE_RETRIES,
                )
                raise
            retry_delay = attempt
            logger.warning(
                "Telemetry page delivery retry sync_id=%s phase=%s page=%d/%d "
                "attempt=%d/%d retry_seconds=%d error=%s",
                sync_id,
                phase,
                page_number + 1,
                page_count,
                attempt,
                PAGE_RETRIES,
                retry_delay,
                type(exc).__name__,
            )
            time.sleep(retry_delay)
    raise AssertionError("unreachable")


def _post_page(
    settings: SyncSettings,
    encoded: EncodedSyncPage,
    *,
    sync_id: str,
    phase: str,
    page_number: int,
    page_count: int,
) -> dict[str, Any]:
    auth_token = str(getattr(settings, "auth_token", "") or "").strip()
    if not auth_token:
        raise ValueError("Telemetry sync requires an authentication token")
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
        "Accept": "application/json",
        "User-Agent": "smARTmatch-telemetry-sync/3",
        "X-Smartmatch-Sync-ID": sync_id,
        "X-Smartmatch-Phase": phase,
        "X-Smartmatch-Page-Number": str(page_number),
        "X-Smartmatch-Page-Count": str(page_count),
        "X-Uncompressed-Content-SHA256": encoded.uncompressed_sha256,
        "X-Uncompressed-Content-Length": str(encoded.uncompressed_bytes),
        "X-Compressed-Content-Length": str(len(encoded.body)),
        "Idempotency-Key": (
            f"smartmatch-sync-{sync_id}-{phase}-{page_number}-"
            f"{encoded.uncompressed_sha256[:24]}"
        ),
    }
    request = Request(
        settings.endpoint, data=encoded.body, headers=headers, method="POST"
    )
    opener = build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=settings.timeout_seconds) as response:
            status = int(response.status)
            response_body = response.read(256 * 1024)
    except HTTPError as exc:
        try:
            detail = exc.read(16 * 1024).decode("utf-8", errors="replace")
        except Exception:
            detail = None
        raise SyncHttpError(int(exc.code), detail) from exc
    if status < 200 or status >= 300:
        raise SyncHttpError(status)
    try:
        acknowledgement = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Telemetry receiver returned an invalid acknowledgement"
        ) from exc
    expected = {
        "sync_id": sync_id,
        "phase": phase,
        "page_number": page_number,
        "payload_sha256": encoded.uncompressed_sha256,
    }
    for key, value in expected.items():
        if acknowledgement.get(key) != value:
            raise ValueError(
                f"Telemetry acknowledgement mismatch for {key}: "
                f"expected {value!r}, got {acknowledgement.get(key)!r}"
            )
    return dict(acknowledgement)


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
