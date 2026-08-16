"""Bounded, opt-in daily telemetry for the matching pipeline."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import ipaddress
import json
import logging
import math
import os
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from matching_pipeline.shared.db import connect
from matching_pipeline.shared.env import (
    env_bool,
    env_image_root,
    env_repo_root,
    env_required_str,
    env_str,
)
from matching_pipeline.shared.telemetry_sync import (
    SyncDeliveryResult,
    deliver_sync_operation,
)

logger = logging.getLogger(__name__)

TELEMETRY_SCHEMA_VERSION = 2
TELEMETRY_WINDOW_DAYS = 7
TELEMETRY_MODULE = "matching_pipeline.shared.telemetry"
DEFAULT_MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_PAYLOAD_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_MATCH_RECORDS = 5_000
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PROCESS_DEADLINE_SECONDS = 2 * 60 * 60
DAEMON_POLL_SECONDS = 1.0
WORKER_LAUNCH_RETRY_SECONDS = 30.0
_MAX_EXTRACTED_TEXT_CHARS = 2_000
_MAX_ARRAY_ITEMS = 50
_HASH_CHUNK_BYTES = 1024 * 1024
_HASH_FETCH_ROWS = 1_000
_MAX_HASH_ERRORS = 20

_DB_ROW_HASH_PREFIX = b"smartmatch-db-rows-v1\0"
_CONTENT_TREE_HASH_PREFIX = b"smartmatch-content-tree-v1\0"
_PATH_SIZE_TREE_HASH_PREFIX = b"smartmatch-path-size-tree-v1\0"
_PATH_SIZE_FILE_HASH_PREFIX = b"smartmatch-path-size-file-v1\0"

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

_RECENT_MATCHES_SQL = f"""
WITH recent_matches AS (
    SELECT ms.*, ({_EFFECTIVE_MATCH_DATE_SQL}) AS effective_match_date
    FROM match_score ms
)
SELECT
    recent.lost_id,
    recent.auction_id,
    recent.effective_match_date AS match_date,
    recent.metadata_match_date,
    recent.image_match_date,
    recent.rating,
    recent.bookmarked,
    recent.title_sim,
    recent.artist_sim,
    recent.dating_sim,
    recent.dimensions_sim,
    recent.material_sim,
    recent.technique_sim,
    recent.metadata_final_score,
    recent.metadata_confidence_score,
    recent.image_matching_confidence,
    recent.image_final_score,
    recent.image_blocking_similarity,
    recent.best_image_file_id,
    metadata_program.name AS metadata_program_name,
    metadata_program.version AS metadata_program_version,
    image_program.name AS image_program_name,
    image_program.version AS image_program_version,
    lost.lost_art_url,
    lost.title AS lost_title,
    lost.dating_start AS lost_dating_start,
    lost.dating_end AS lost_dating_end,
    lost.width AS lost_width,
    lost.height AS lost_height,
    lost.dict_material_name AS lost_material_terms,
    lost.dict_technique_name AS lost_technique_terms,
    COALESCE(lost_artists.names, ARRAY[]::text[]) AS lost_artist_names,
    auction.lot_url AS auction_lot_url,
    auction.title AS extracted_title,
    auction.artist_full_name AS extracted_author,
    auction.date_of_birth_raw_data AS extracted_date_of_birth,
    auction.place_of_birth_raw_data AS extracted_place_of_birth,
    auction.date_of_death_raw_data AS extracted_date_of_death,
    auction.place_of_death_raw_data AS extracted_place_of_death,
    auction.dimensions_raw_data AS extracted_dimensions,
    auction.dating AS extracted_dating,
    auction.dating_start AS extracted_dating_start,
    auction.dating_end AS extracted_dating_end,
    auction.width AS extracted_width,
    auction.height AS extracted_height,
    auction.width_frame AS extracted_width_frame,
    auction.height_frame AS extracted_height_frame,
    auction.material AS extracted_material,
    auction.dict_material_name AS extracted_material_terms,
    auction.technique AS extracted_technique,
    auction.dict_technique_name AS extracted_technique_terms,
    auction.provenance AS extracted_provenance,
    auction.signature AS extracted_signature,
    auction.condition AS extracted_condition,
    auction.literature AS extracted_literature,
    best_auction_image.file_path AS best_auction_image_path,
    best_lost_image.image_file_id AS best_lost_image_file_id,
    best_lost_image.file_path AS best_lost_image_path
FROM recent_matches recent
JOIN lost_artwork lost ON lost.lost_artwork_id = recent.lost_id
JOIN auction_artwork auction ON auction.auction_artwork_id = recent.auction_id
LEFT JOIN matching_program metadata_program
    ON metadata_program.matching_program_id = recent.metadata_matching_program
LEFT JOIN matching_program image_program
    ON image_program.matching_program_id = recent.image_matching_program
LEFT JOIN image_file best_auction_image
    ON best_auction_image.image_file_id = recent.best_image_file_id
LEFT JOIN image_file best_lost_image
    ON best_lost_image.image_file_id::text =
       recent.image_visualization #>> '{{image_matching,best_match,lost_image_file_id}}'
LEFT JOIN LATERAL (
    SELECT array_agg(artist.complete_name::text ORDER BY artist.complete_name) AS names
    FROM artist
    WHERE artist.artist_id = ANY(lost.artist_ids)
) lost_artists ON true
WHERE recent.effective_match_date >= %s
  AND recent.effective_match_date < %s
ORDER BY recent.effective_match_date DESC, recent.lost_id, recent.auction_id
LIMIT %s
"""

_RECENT_MATCH_COUNT_SQL = f"""
WITH recent_matches AS (
    SELECT ({_EFFECTIVE_MATCH_DATE_SQL}) AS effective_match_date
    FROM match_score ms
)
SELECT COUNT(*)::bigint
FROM recent_matches
WHERE effective_match_date >= %s
  AND effective_match_date < %s
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

_EXTRACTED_COLUMNS = {
    "title": "extracted_title",
    "author": "extracted_author",
    "date_of_birth": "extracted_date_of_birth",
    "place_of_birth": "extracted_place_of_birth",
    "date_of_death": "extracted_date_of_death",
    "place_of_death": "extracted_place_of_death",
    "dimensions": "extracted_dimensions",
    "dating": "extracted_dating",
    "dating_start": "extracted_dating_start",
    "dating_end": "extracted_dating_end",
    "width": "extracted_width",
    "height": "extracted_height",
    "width_frame": "extracted_width_frame",
    "height_frame": "extracted_height_frame",
    "material": "extracted_material",
    "material_terms": "extracted_material_terms",
    "technique": "extracted_technique",
    "technique_terms": "extracted_technique_terms",
    "provenance": "extracted_provenance",
    "signature": "extracted_signature",
    "condition": "extracted_condition",
    "literature": "extracted_literature",
}


@dataclass(frozen=True)
class TelemetrySettings:
    endpoint: str
    auth_token: str
    image_root: Path
    project_root: Path
    timeout_seconds: float
    match_expiration_seconds: int


@dataclass(frozen=True)
class DirectoryHash:
    sha256: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class TreeHashSnapshot:
    root: DirectoryHash
    subdirectories: Mapping[str, DirectoryHash]
    file_hashes: Mapping[str, str]
    hash_basis: str
    error_count: int
    errors: Sequence[str]


@dataclass(frozen=True)
class EncodedPayload:
    body: bytes
    content_sha256: str
    uncompressed_bytes: int
    included_match_count: int
    total_match_count: int
    truncated: bool


class TelemetryHttpError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"Telemetry endpoint returned HTTP {status}")
        self.status = status


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_local_telemetry_host(hostname: str) -> bool:
    hostname = hostname.rstrip(".").lower()
    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname == "host.docker.internal"
        or "." not in hostname
    ):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    hostname,
                    None,
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror:
            return False
    else:
        addresses = {str(address)}
    return bool(addresses) and all(
        (
            ipaddress.ip_address(address).is_private
            or ipaddress.ip_address(address).is_loopback
            or ipaddress.ip_address(address).is_link_local
        )
        for address in addresses
    )


def load_telemetry_settings() -> TelemetrySettings | None:
    """Return validated settings, or ``None`` when telemetry is disabled."""
    if not env_bool("TELEMETRY_ENABLED"):
        return None

    endpoint = env_required_str("TELEMETRY_ENDPOINT")
    parsed = urlsplit(endpoint)
    scheme = parsed.scheme.lower()
    if not parsed.hostname:
        raise ValueError("TELEMETRY_ENDPOINT must be an absolute URL")
    insecure_local_http = env_bool("TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP")
    if scheme != "https" and not (
        scheme == "http"
        and insecure_local_http
        and _is_local_telemetry_host(parsed.hostname)
    ):
        raise ValueError(
            "TELEMETRY_ENDPOINT must use HTTPS; insecure HTTP is allowed only "
            "for local hosts when TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP=true"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("TELEMETRY_ENDPOINT must not contain URL credentials")
    if parsed.fragment:
        raise ValueError("TELEMETRY_ENDPOINT must not contain a fragment")
    auth_token = env_required_str("TELEMETRY_AUTH_TOKEN")

    timeout_text = env_str("TELEMETRY_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    assert timeout_text is not None
    try:
        timeout_seconds = float(timeout_text)
    except ValueError as exc:
        raise ValueError("TELEMETRY_TIMEOUT_SECONDS must be a number") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("TELEMETRY_TIMEOUT_SECONDS must be greater than zero")

    project_root_text = env_str("SMARTMATCH_PROJECT_DIR")
    project_root = (
        Path(project_root_text).expanduser().resolve()
        if project_root_text
        else env_repo_root()
    )
    return TelemetrySettings(
        endpoint=endpoint,
        auth_token=auth_token,
        image_root=env_image_root(),
        project_root=project_root,
        timeout_seconds=timeout_seconds,
        match_expiration_seconds=_duration_seconds(
            env_str("SMARTMATCH_MATCH_EXPIRATION_AGE", "30d") or "30d"
        ),
    )


def try_send_daily_telemetry(now: datetime | None = None) -> str:
    """Attempt at most one telemetry delivery per UTC day, without raising."""
    try:
        settings = load_telemetry_settings()
    except Exception:
        logger.exception("Telemetry configuration is invalid; skipping daily attempt")
        return "invalid_configuration"
    if settings is None:
        logger.info("Daily telemetry attempt skipped: telemetry is disabled")
        return "disabled"

    current = _as_utc(now or datetime.now(timezone.utc))
    attempt_date = current.date()
    logger.info("Daily telemetry attempt started attempt_date=%s", attempt_date)
    try:
        if not _claim_daily_attempt(attempt_date):
            logger.info(
                "Daily telemetry attempt skipped attempt_date=%s status=already_attempted",
                attempt_date,
            )
            return "already_attempted"
    except Exception:
        logger.exception(
            "Could not claim the daily telemetry attempt attempt_date=%s",
            attempt_date,
        )
        return "claim_failed"

    try:
        summary = collect_telemetry_payload(
            settings,
            generated_at=current,
            trigger="daily",
        )
        summary.pop("recent_matches", None)
        logger.info(
            "Daily telemetry snapshot ready attempt_date=%s; starting delivery",
            attempt_date,
        )
        result = deliver_sync_operation(
            settings,
            trigger="daily",
            generated_at=current,
            summary=summary,
        )
    except Exception as exc:
        _record_daily_result(
            attempt_date,
            status="failed",
            encoded=None,
            http_status=getattr(exc, "status", None),
            error_class=type(exc).__name__,
        )
        logger.exception("Daily telemetry sync failed attempt_date=%s", attempt_date)
        return "failed"

    result_recorded = _record_daily_sync_result(attempt_date, result)
    if not result_recorded:
        logger.error(
            "Telemetry was delivered, but its daily result could not be persisted"
        )
    logger.info(
        "Daily telemetry sync sent: sync_id=%s pages=%d compressed_bytes=%d",
        result.sync_id,
        result.page_count,
        result.total_compressed_bytes,
    )
    return "sent"


def try_send_startup_telemetry(now: datetime | None = None) -> str:
    """Attempt one delivery for this scheduler/container start, without raising."""
    try:
        settings = load_telemetry_settings()
    except Exception:
        logger.exception("Telemetry configuration is invalid; skipping startup attempt")
        return "invalid_configuration"
    if settings is None:
        logger.info("Startup telemetry attempt skipped: telemetry is disabled")
        return "disabled"

    current = _as_utc(now or datetime.now(timezone.utc))
    logger.info(
        "Startup telemetry attempt started generated_at=%s", _isoformat(current)
    )
    try:
        summary = collect_telemetry_payload(
            settings,
            generated_at=current,
            trigger="startup",
        )
        summary.pop("recent_matches", None)
        logger.info("Startup telemetry snapshot ready; starting delivery")
        result = deliver_sync_operation(
            settings,
            trigger="startup",
            generated_at=current,
            summary=summary,
        )
    except Exception:
        logger.exception("Startup telemetry sync failed")
        return "failed"

    logger.info(
        "Startup telemetry sync sent: sync_id=%s pages=%d compressed_bytes=%d",
        result.sync_id,
        result.page_count,
        result.total_compressed_bytes,
    )
    return "sent"


def collect_telemetry_payload(
    settings: TelemetrySettings,
    *,
    generated_at: datetime,
    trigger: str,
) -> dict[str, Any]:
    """Collect one fixed-window database and filesystem snapshot."""
    window_end = _as_utc(generated_at)
    window_start = window_end - timedelta(days=TELEMETRY_WINDOW_DAYS)
    logger.info(
        "Telemetry snapshot collection started trigger=%s window_start=%s window_end=%s",
        trigger,
        _isoformat(window_start),
        _isoformat(window_end),
    )
    database = _collect_database_snapshot(
        window_start=window_start,
        window_end=window_end,
        expiration_seconds=settings.match_expiration_seconds,
        max_match_records=DEFAULT_MAX_MATCH_RECORDS,
    )
    matches = database.pop("recent_matches")
    total_matches = database.pop("recent_match_total")
    logger.info(
        "Telemetry database snapshot complete trigger=%s recent_matches=%d/%d",
        trigger,
        len(matches),
        total_matches,
    )
    retained_image_paths = _match_image_paths(matches, settings.image_root)
    logger.info(
        "Telemetry image hashing started root=%s retained_match_images=%d",
        settings.image_root,
        len(retained_image_paths),
    )
    image_snapshot = hash_directory_tree(
        settings.image_root,
        retain_file_paths=retained_image_paths,
        hash_file_contents=False,
    )
    logger.info(
        "Telemetry image hashing complete files=%d bytes=%d read_errors=%d",
        image_snapshot.root.file_count,
        image_snapshot.root.total_bytes,
        image_snapshot.error_count,
    )
    _attach_match_image_hashes(matches, image_snapshot, settings.image_root)

    logger.info("Telemetry runtime metadata collection started")
    git = _git_identity(settings.project_root)
    runtime = _runtime_reproducibility_metadata()
    payload = {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "trigger": trigger,
        "generated_at": _isoformat(window_end),
        "window": {
            "days": TELEMETRY_WINDOW_DAYS,
            "start": _isoformat(window_start),
            "end": _isoformat(window_end),
        },
        "git": git,
        "datasets": {
            "lost_artwork": database.pop("lost_artwork_hash"),
            "reproducibility_dependencies": database.pop("dependency_hashes"),
            "images": _tree_snapshot_payload(image_snapshot),
        },
        "counts": database.pop("counts"),
        "match_categories": database.pop("match_categories"),
        "last_auction_artwork_added_per_scraper": database.pop("scraper_dates"),
        "reproducibility": {
            "matching_programs": database.pop("matching_programs"),
            "runtime": runtime,
        },
        "recent_matches": {
            "total_count": total_matches,
            "included_count": len(matches),
            "truncated": len(matches) < total_matches,
            "selection": "newest_first",
            "records": matches,
        },
    }
    if database:
        raise AssertionError(f"Unhandled telemetry database fields: {sorted(database)}")
    logger.info(
        "Telemetry snapshot collection complete trigger=%s recent_matches=%d/%d",
        trigger,
        len(matches),
        total_matches,
    )
    return payload


def hash_directory_tree(
    root: Path,
    *,
    retain_file_paths: set[str] | None = None,
    hash_file_contents: bool = True,
) -> TreeHashSnapshot:
    """Hash a tree deterministically without following symlinks.

    Content mode is retained for small reproducibility artifacts. Image callers
    use path/size mode so telemetry never opens or reads image file contents.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Telemetry image directory does not exist: {root}")
    file_hashes: dict[str, str] = {}
    errors: list[str] = []
    error_count = 0
    subdirectories: dict[str, DirectoryHash] = {}

    def record_error(relative_path: str, exc: OSError) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < _MAX_HASH_ERRORS:
            errors.append(f"{relative_path}:{type(exc).__name__}")

    hash_basis = "file_content" if hash_file_contents else "relative_path_and_size"
    tree_prefix = (
        _CONTENT_TREE_HASH_PREFIX if hash_file_contents else _PATH_SIZE_TREE_HASH_PREFIX
    )

    def visit(directory: Path, relative: Path) -> DirectoryHash:
        digest = hashlib.sha256(tree_prefix)
        file_count = 0
        total_bytes = 0
        try:
            entries = sorted(
                directory.iterdir(), key=lambda path: os.fsencode(path.name)
            )
        except OSError as exc:
            record_error(relative.as_posix() or ".", exc)
            digest.update(b"unreadable-directory\0")
            return DirectoryHash(digest.hexdigest(), 0, 0)

        for entry in entries:
            entry_relative = relative / entry.name
            name = os.fsencode(entry.name)
            try:
                if entry.is_symlink():
                    target_hash = hashlib.sha256(
                        os.fsencode(os.readlink(entry))
                    ).hexdigest()
                    digest.update(_tree_record(b"L", name, 0, target_hash))
                elif entry.is_dir():
                    child = visit(entry, entry_relative)
                    digest.update(
                        _tree_record(b"D", name, child.total_bytes, child.sha256)
                    )
                    file_count += child.file_count
                    total_bytes += child.total_bytes
                    if relative == Path():
                        subdirectories[entry.name] = child
                elif entry.is_file():
                    relative_text = entry_relative.as_posix()
                    if hash_file_contents:
                        file_hash, size = _hash_file(entry)
                    else:
                        size = entry.stat(follow_symlinks=False).st_size
                        file_hash = _path_size_file_hash(relative_text, size)
                    digest.update(_tree_record(b"F", name, size, file_hash))
                    if retain_file_paths is None or relative_text in retain_file_paths:
                        file_hashes[relative_text] = file_hash
                    file_count += 1
                    total_bytes += size
                else:
                    digest.update(_tree_record(b"O", name, 0, ""))
            except OSError as exc:
                record_error(entry_relative.as_posix(), exc)
                error_hash = hashlib.sha256(
                    type(exc).__name__.encode("ascii")
                ).hexdigest()
                digest.update(_tree_record(b"E", name, 0, error_hash))
        return DirectoryHash(digest.hexdigest(), file_count, total_bytes)

    root_hash = visit(root, Path())
    return TreeHashSnapshot(
        root=root_hash,
        subdirectories=dict(sorted(subdirectories.items())),
        file_hashes=file_hashes,
        hash_basis=hash_basis,
        error_count=error_count,
        errors=tuple(errors),
    )


def encode_bounded_payload(
    payload: Mapping[str, Any],
    max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_PAYLOAD_BYTES,
    max_match_records: int = DEFAULT_MAX_MATCH_RECORDS,
) -> EncodedPayload:
    """Bound and gzip a payload, retaining the newest recent matches first."""
    if min(max_bytes, max_uncompressed_bytes, max_match_records) <= 0:
        raise ValueError("Telemetry payload limits must be positive")
    working = dict(payload)
    recent = dict(working.get("recent_matches") or {})
    available_records = list(recent.get("records") or [])
    total = int(recent.get("total_count", len(available_records)))
    total = max(total, len(available_records))
    records = available_records[:max_match_records]
    recent.update(
        {
            "total_count": total,
            "candidate_count": len(records),
            "candidate_records_sha256": _canonical_sha256(records),
            "included_count": len(records),
            "included_records_sha256": _canonical_sha256(records),
            "truncated": len(records) < total,
            "omitted_count": total - len(records),
            "records": records,
        }
    )
    working["recent_matches"] = recent

    body, content_hash, uncompressed_bytes = _encode_json_gzip(working)
    if len(body) <= max_bytes and uncompressed_bytes <= max_uncompressed_bytes:
        return EncodedPayload(
            body,
            content_hash,
            uncompressed_bytes,
            len(records),
            total,
            len(records) < total,
        )

    low = 0
    high = len(records)
    best: tuple[bytes, str, int, int] | None = None
    while low <= high:
        included = (low + high) // 2
        candidate = dict(working)
        candidate_recent = dict(recent)
        candidate_recent.update(
            {
                "included_count": included,
                "included_records_sha256": _canonical_sha256(records[:included]),
                "truncated": included < total,
                "omitted_count": total - included,
                "records": records[:included],
            }
        )
        candidate["recent_matches"] = candidate_recent
        candidate_body, candidate_hash, candidate_uncompressed = _encode_json_gzip(
            candidate
        )
        if (
            len(candidate_body) <= max_bytes
            and candidate_uncompressed <= max_uncompressed_bytes
        ):
            best = (
                candidate_body,
                candidate_hash,
                candidate_uncompressed,
                included,
            )
            low = included + 1
        else:
            high = included - 1

    if best is None:
        raise ValueError(
            "Telemetry metadata exceeds the fixed payload limits even without "
            "recent match records"
        )
    body, content_hash, uncompressed_bytes, included = best
    return EncodedPayload(
        body,
        content_hash,
        uncompressed_bytes,
        included,
        total,
        included < total,
    )


def _collect_database_snapshot(
    *,
    window_start: datetime,
    window_end: datetime,
    expiration_seconds: int,
    max_match_records: int,
) -> dict[str, Any]:
    logger.info("Telemetry database snapshot started")
    conn = connect(connect_timeout=10)
    try:
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
            cur.execute(_RECENT_MATCH_COUNT_SQL, (window_start, window_end))
            recent_match_total = int(cur.fetchone()[0])
            cur.execute(
                _RECENT_MATCHES_SQL,
                (window_start, window_end, max_match_records),
            )
            recent_rows = _fetchall_dicts(cur)
        conn.rollback()
        logger.info(
            "Telemetry database aggregate queries complete recent_matches=%d/%d",
            len(recent_rows),
            recent_match_total,
        )
    finally:
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
        "recent_match_total": recent_match_total,
        "recent_matches": [
            _recent_match_payload(row, window_end, expiration_seconds)
            for row in recent_rows
        ],
    }


def _recent_match_payload(
    row: Mapping[str, Any],
    now: datetime,
    expiration_seconds: int,
) -> dict[str, Any]:
    match_date = row.get("match_date")
    rating = int(row.get("rating") or 0)
    bookmarked = bool(row.get("bookmarked"))
    expired = bool(
        isinstance(match_date, datetime)
        and _as_utc(match_date) < now - timedelta(seconds=expiration_seconds)
    )
    extracted, truncated_fields = _compact_extracted_fields(row)
    lost_artists = list(row.get("lost_artist_names") or [])
    lost_material_terms = list(row.get("lost_material_terms") or [])
    lost_technique_terms = list(row.get("lost_technique_terms") or [])
    input_truncation = {
        name: len(values)
        for name, values in (
            ("lost_artists", lost_artists),
            ("lost_material_terms", lost_material_terms),
            ("lost_technique_terms", lost_technique_terms),
        )
        if len(values) > _MAX_ARRAY_ITEMS
    }
    result = {
        "lost_artwork_id": str(row["lost_id"]),
        "auction_artwork_id": str(row["auction_id"]),
        "source_urls": {
            "lost_artwork": _bounded_text(row.get("lost_art_url")),
            "auction_artwork": _bounded_text(row.get("auction_lot_url")),
        },
        "match_dates": {
            "effective": _json_safe(match_date),
            "metadata": _json_safe(row.get("metadata_match_date")),
            "image": _json_safe(row.get("image_match_date")),
        },
        "review": {
            "rating": rating,
            "bookmarked": bookmarked,
            "new": rating == 0 and not bookmarked,
            "accepted": rating > 0,
            "discarded": rating < 0,
            "expired": expired,
        },
        "scores": {
            key: _json_safe(row.get(key))
            for key in (
                "title_sim",
                "artist_sim",
                "dating_sim",
                "dimensions_sim",
                "material_sim",
                "technique_sim",
                "metadata_final_score",
                "metadata_confidence_score",
                "image_matching_confidence",
                "image_final_score",
                "image_blocking_similarity",
            )
        },
        "programs": {
            "metadata": {
                "name": row.get("metadata_program_name"),
                "version": row.get("metadata_program_version"),
            },
            "image": {
                "name": row.get("image_program_name"),
                "version": row.get("image_program_version"),
            },
        },
        "matching_inputs": {
            "lost": {
                "title": _safe_match_text(row.get("lost_title")),
                "artists": [
                    _safe_match_text(value) for value in lost_artists[:_MAX_ARRAY_ITEMS]
                ],
                "dating_start": row.get("lost_dating_start"),
                "dating_end": row.get("lost_dating_end"),
                "width": _json_safe(row.get("lost_width")),
                "height": _json_safe(row.get("lost_height")),
                "material_terms": _json_safe(lost_material_terms[:_MAX_ARRAY_ITEMS]),
                "technique_terms": _json_safe(lost_technique_terms[:_MAX_ARRAY_ITEMS]),
            },
            "auction": {
                "title": _safe_match_text(row.get("extracted_title")),
                "artist": _safe_match_text(row.get("extracted_author")),
                "dating_start": row.get("extracted_dating_start"),
                "dating_end": row.get("extracted_dating_end"),
                "width": _json_safe(row.get("extracted_width")),
                "height": _json_safe(row.get("extracted_height")),
                "material_terms": _json_safe(
                    list(row.get("extracted_material_terms") or [])[:10]
                ),
                "technique_terms": _json_safe(
                    list(row.get("extracted_technique_terms") or [])[:10]
                ),
            },
        },
        "extracted_fields": extracted,
        "image_evidence": {
            "best_auction_image_file_id": row.get("best_image_file_id"),
            "best_lost_image_file_id": row.get("best_lost_image_file_id"),
            "best_auction_image_path_size_sha256": None,
            "best_lost_image_path_size_sha256": None,
        },
        "_image_paths": {
            "auction": row.get("best_auction_image_path"),
            "lost": row.get("best_lost_image_path"),
        },
    }
    if truncated_fields:
        result["extracted_fields_truncated"] = truncated_fields
    if input_truncation:
        result["matching_inputs_truncated"] = {
            name: {"original_items": count} for name, count in input_truncation.items()
        }
    return _json_safe(result)


def _compact_extracted_fields(
    row: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    values: dict[str, Any] = {}
    truncated: dict[str, Any] = {}
    for public_name, column_name in _EXTRACTED_COLUMNS.items():
        value = row.get(column_name)
        if isinstance(value, str) and len(value) > _MAX_EXTRACTED_TEXT_CHARS:
            encoded = value.encode("utf-8")
            values[public_name] = value[:_MAX_EXTRACTED_TEXT_CHARS]
            truncated[public_name] = {
                "original_chars": len(value),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }
        elif isinstance(value, (list, tuple)) and len(value) > _MAX_ARRAY_ITEMS:
            values[public_name] = _json_safe(value[:_MAX_ARRAY_ITEMS])
            truncated[public_name] = {
                "original_items": len(value),
                "sha256": _canonical_sha256(value),
            }
        else:
            values[public_name] = _json_safe(value)
    return values, truncated


def _match_image_paths(
    matches: Sequence[Mapping[str, Any]], image_root: Path
) -> set[str]:
    paths: set[str] = set()
    for match in matches:
        for value in (match.get("_image_paths") or {}).values():
            relative = _relative_image_path(value, image_root)
            if relative is not None:
                paths.add(relative)
    return paths


def _attach_match_image_hashes(
    matches: Sequence[dict[str, Any]],
    snapshot: TreeHashSnapshot,
    image_root: Path,
) -> None:
    for match in matches:
        paths = match.pop("_image_paths", {})
        evidence = match["image_evidence"]
        for role in ("auction", "lost"):
            relative = _relative_image_path(paths.get(role), image_root)
            evidence[f"best_{role}_image_path_size_sha256"] = (
                snapshot.file_hashes.get(relative) if relative is not None else None
            )


def _relative_image_path(value: object, image_root: Path) -> str | None:
    if value is None:
        return None
    raw = Path(str(value)).expanduser()
    if raw.is_absolute():
        try:
            return raw.resolve().relative_to(image_root.resolve()).as_posix()
        except (OSError, ValueError):
            return None
    parts = raw.parts
    if "images" in parts:
        index = len(parts) - 1 - list(reversed(parts)).index("images")
        parts = parts[index + 1 :]
    candidate = Path(*parts)
    if not parts or ".." in candidate.parts:
        return None
    return candidate.as_posix()


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
    encoded: EncodedPayload | None,
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
                    """,
                    (
                        status,
                        encoded.content_sha256 if encoded else None,
                        len(encoded.body) if encoded else None,
                        http_status,
                        error_class,
                        attempt_date,
                    ),
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


def _post_payload(
    settings: TelemetrySettings,
    encoded: EncodedPayload,
    *,
    idempotency_scope: str,
) -> int:
    headers = {
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
        "Accept": "application/json",
        "User-Agent": "smARTmatch-telemetry/1",
        "X-Uncompressed-Content-SHA256": encoded.content_sha256,
        "X-Uncompressed-Content-Length": str(encoded.uncompressed_bytes),
        "X-Compressed-Content-Length": str(len(encoded.body)),
        "Idempotency-Key": f"smartmatch-telemetry-{idempotency_scope}-{encoded.content_sha256[:24]}",
    }
    request = Request(
        settings.endpoint, data=encoded.body, headers=headers, method="POST"
    )
    opener = build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=settings.timeout_seconds) as response:
            status = int(response.status)
    except HTTPError as exc:
        raise TelemetryHttpError(int(exc.code)) from exc
    if status < 200 or status >= 300:
        raise TelemetryHttpError(status)
    return status


def _tree_snapshot_payload(snapshot: TreeHashSnapshot) -> dict[str, Any]:
    return {
        "algorithm": (
            "sha256-merkle-content-tree-v1"
            if snapshot.hash_basis == "file_content"
            else "sha256-merkle-path-size-tree-v1"
        ),
        "hash_basis": snapshot.hash_basis,
        "sha256": snapshot.root.sha256,
        "file_count": snapshot.root.file_count,
        "total_bytes": snapshot.root.total_bytes,
        "subdirectories": {
            name: {
                "sha256": item.sha256,
                "file_count": item.file_count,
                "total_bytes": item.total_bytes,
            }
            for name, item in snapshot.subdirectories.items()
        },
        "read_complete": snapshot.error_count == 0,
        "consistency": "live_best_effort",
        "error_count": snapshot.error_count,
        "errors": list(snapshot.errors),
    }


def _runtime_reproducibility_metadata() -> dict[str, Any]:
    package_root = Path(__file__).resolve().parents[1]
    classifier = package_root / "image_matching" / "classifier.pkl"
    reference_root = package_root / "shared" / "reference_data"
    result: dict[str, Any] = {
        "models": {
            "dinov3": env_str("DINOV3_MODEL_ID"),
            "metadata_backend": env_str("METADATA_BACKEND"),
            "metadata_model": env_str("METADATA_MODEL"),
            "metadata_quantization": env_str("METADATA_QUANTIZATION"),
        },
        "configuration": {
            "matching_batch_size": env_str("MATCHING_BATCH_SIZE"),
            "max_similarity_string_length": env_str("MAX_SIM_STRING_LEN", "100"),
        },
        "artifacts": {},
        "packages": {},
    }
    result["artifacts"]["runtime_python_source_sha256"] = _runtime_source_hash(
        package_root
    )
    if classifier.is_file():
        result["artifacts"]["image_classifier_sha256"] = _hash_file(classifier)[0]
    if reference_root.is_dir():
        result["artifacts"]["reference_data_sha256"] = hash_directory_tree(
            reference_root
        ).root.sha256
    for package in ("torch", "transformers", "kornia", "scikit-learn", "pyarrow"):
        try:
            result["packages"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result["packages"][package] = None
    return result


def _git_identity(project_root: Path) -> dict[str, Any]:
    commit = _run_git(project_root, "rev-parse", "HEAD")
    source = "git"
    if commit is None:
        commit = env_str("SMARTMATCH_GIT_COMMIT")
        source = "environment" if commit else "unavailable"
    dirty_output = (
        _run_git(project_root, "status", "--porcelain", "--untracked-files=no")
        if (project_root / "matching_pipeline").is_dir()
        else None
    )
    return {
        "commit": commit,
        "source": source,
        "tracked_files_dirty": None if dirty_output is None else bool(dirty_output),
    }


def _runtime_source_hash(package_root: Path) -> str:
    app_root = package_root.parent
    paths = sorted(
        path for path in package_root.rglob("*.py") if "__pycache__" not in path.parts
    )
    scheduler = app_root / "scripts" / "run_pipeline_scheduler.py"
    if scheduler.is_file():
        paths.append(scheduler)
    digest = hashlib.sha256(b"smartmatch-runtime-python-v1\0")
    for path in sorted(paths):
        relative = path.relative_to(app_root).as_posix().encode("utf-8")
        file_hash, size = _hash_file(path)
        digest.update(_tree_record(b"F", relative, size, file_hash))
    return digest.hexdigest()


def _run_git(project_root: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _path_size_file_hash(relative_path: str, size: int) -> str:
    digest = hashlib.sha256(_PATH_SIZE_FILE_HASH_PREFIX)
    encoded_path = relative_path.encode("utf-8", errors="surrogateescape")
    digest.update(str(len(encoded_path)).encode("ascii"))
    digest.update(b"\0")
    digest.update(encoded_path)
    digest.update(b"\0")
    digest.update(str(size).encode("ascii"))
    return digest.hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    before_identity = (before.st_size, before.st_mtime_ns, before.st_ino)
    after_identity = (after.st_size, after.st_mtime_ns, after.st_ino)
    if before_identity != after_identity:
        raise OSError("file changed while being hashed")
    return digest.hexdigest(), after.st_size


def _tree_record(kind: bytes, name: bytes, size: int, digest: str) -> bytes:
    return (
        b"\0".join(
            (
                kind,
                str(len(name)).encode("ascii"),
                name,
                str(size).encode("ascii"),
                digest.encode("ascii"),
            )
        )
        + b"\0"
    )


def _encode_json_gzip(payload: Mapping[str, Any]) -> tuple[bytes, str, int]:
    raw = json.dumps(
        _json_safe(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return (
        gzip.compress(raw, compresslevel=6, mtime=0),
        hashlib.sha256(raw).hexdigest(),
        len(raw),
    )


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _bounded_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)[:_MAX_EXTRACTED_TEXT_CHARS]


def _safe_match_text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)[:100]


def _duration_seconds(value: str) -> int:
    text = value.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(text) < 2 or text[-1] not in units:
        raise ValueError(
            "SMARTMATCH_MATCH_EXPIRATION_AGE must be a positive duration such as 30d"
        )
    try:
        amount = float(text[:-1])
    except ValueError as exc:
        raise ValueError(
            "SMARTMATCH_MATCH_EXPIRATION_AGE must be a positive duration such as 30d"
        ) from exc
    seconds = amount * units[text[-1]]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("SMARTMATCH_MATCH_EXPIRATION_AGE must be greater than zero")
    return int(seconds)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _isoformat(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def run_telemetry_daemon(
    stop_event: Event,
    *,
    now_fn=None,
    launch_worker=None,
) -> int:
    """Schedule startup and UTC-daily attempts independently of the matcher."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    launch_worker = launch_worker or _launch_worker
    startup_pending = True
    last_daily_started: date | None = None
    worker: subprocess.Popen[bytes] | None = None
    worker_trigger: str | None = None
    last_enabled: bool | None = None

    logger.info(
        "Telemetry daemon started poll_seconds=%.0f retry_seconds=%.0f",
        DAEMON_POLL_SECONDS,
        WORKER_LAUNCH_RETRY_SECONDS,
    )
    while not stop_event.is_set():
        enabled = env_bool("TELEMETRY_ENABLED")
        if enabled != last_enabled:
            logger.info("Telemetry daemon configuration enabled=%s", enabled)
            last_enabled = enabled

        if worker is not None and worker.poll() is not None:
            return_code = worker.wait()
            if return_code == 0:
                logger.info(
                    "Telemetry worker trigger=%s finished exit_code=%d",
                    worker_trigger,
                    return_code,
                )
            else:
                logger.error(
                    "Telemetry worker trigger=%s failed exit_code=%d",
                    worker_trigger,
                    return_code,
                )
            worker = None
            if worker_trigger == "startup":
                if return_code == 0:
                    startup_pending = False
                else:
                    logger.warning(
                        "Startup telemetry did not complete; retrying in %.0f seconds",
                        WORKER_LAUNCH_RETRY_SECONDS,
                    )
                    worker_trigger = None
                    stop_event.wait(WORKER_LAUNCH_RETRY_SECONDS)
                    continue
            worker_trigger = None

        if worker is None and enabled:
            trigger: str | None = None
            current_date = _as_utc(now_fn()).date()
            if startup_pending:
                trigger = "startup"
            elif current_date != last_daily_started:
                trigger = "daily"

            if trigger is not None:
                logger.info(
                    "Telemetry worker launch started trigger=%s date=%s",
                    trigger,
                    current_date,
                )
                worker = launch_worker(trigger)
                if worker is None:
                    logger.error(
                        "Telemetry worker launch failed trigger=%s; retrying in %.0f seconds",
                        trigger,
                        WORKER_LAUNCH_RETRY_SECONDS,
                    )
                    stop_event.wait(WORKER_LAUNCH_RETRY_SECONDS)
                    continue
                worker_trigger = trigger
                logger.info(
                    "Telemetry worker running trigger=%s pid=%s",
                    trigger,
                    getattr(worker, "pid", "unknown"),
                )
                if trigger == "daily":
                    last_daily_started = current_date

        stop_event.wait(DAEMON_POLL_SECONDS)

    logger.info("Telemetry daemon stopping active_trigger=%s", worker_trigger)
    if worker is not None:
        _stop_worker(worker)
    logger.info("Telemetry daemon stopped")
    return 0


def _launch_worker(trigger: str) -> subprocess.Popen[bytes] | None:
    try:
        return subprocess.Popen(
            (sys.executable, "-m", TELEMETRY_MODULE, "--trigger", trigger),
            cwd=env_repo_root(),
            env=os.environ.copy(),
            start_new_session=True,
        )
    except OSError:
        logger.exception("Could not start %s telemetry worker", trigger)
        return None


def _stop_worker(worker: subprocess.Popen[bytes]) -> None:
    if worker.poll() is not None:
        worker.wait()
        return
    try:
        os.killpg(worker.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        worker.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        worker.wait()


def _run_one_shot(trigger: str) -> int:
    def deadline_reached(_signum: int, _frame: object) -> None:
        logger.error(
            "Telemetry worker trigger=%s exceeded deadline_seconds=%d",
            trigger,
            DEFAULT_PROCESS_DEADLINE_SECONDS,
        )
        logging.shutdown()
        os._exit(124)

    logger.info(
        "Telemetry worker started trigger=%s deadline_seconds=%d",
        trigger,
        DEFAULT_PROCESS_DEADLINE_SECONDS,
    )
    alarm_available = hasattr(signal, "SIGALRM")
    previous_handler = None
    if alarm_available:
        previous_handler = signal.signal(signal.SIGALRM, deadline_reached)
        signal.alarm(DEFAULT_PROCESS_DEADLINE_SECONDS)
    try:
        if trigger == "startup":
            outcome = try_send_startup_telemetry()
        else:
            outcome = try_send_daily_telemetry()
        logger.info("Telemetry worker finished trigger=%s outcome=%s", trigger, outcome)
    finally:
        if alarm_available:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
    successful_outcomes = {"sent", "already_attempted"}
    return 0 if outcome in successful_outcomes else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run the independent startup/daily telemetry scheduler.",
    )
    parser.add_argument(
        "--trigger",
        choices=("startup", "daily"),
        default="daily",
        help="One-shot trigger used by daemon worker processes.",
    )
    return parser.parse_args()


def main() -> int:
    from shared.logging_adapter import configure_logging

    configure_logging()
    args = _parse_args()
    if not args.daemon:
        return _run_one_shot(args.trigger)

    stop_event = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return run_telemetry_daemon(stop_event)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
