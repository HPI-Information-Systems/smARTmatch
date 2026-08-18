"""Assembly of aggregate database and runtime telemetry summaries."""

from datetime import datetime, timedelta
from typing import Any

from telemetry.constants import TELEMETRY_SCHEMA_VERSION, TELEMETRY_WINDOW_DAYS, logger
from telemetry.database import _collect_database_snapshot
from telemetry.models import (
    NonDatabaseTelemetrySnapshot,
    TelemetrySettings,
    TreeHashSnapshot,
)
from telemetry.provenance import _git_identity, _runtime_reproducibility_metadata
from telemetry.serialization import _as_utc, _isoformat
from telemetry.tree_hashing import hash_directory_tree


def _collect_non_database_telemetry(
    settings: TelemetrySettings,
) -> NonDatabaseTelemetrySnapshot:
    logger.info("Telemetry image hashing started root=%s", settings.image_root)
    image_snapshot = hash_directory_tree(
        settings.image_root,
        retain_file_paths=set(),
        hash_file_contents=False,
    )
    logger.info(
        "Telemetry image hashing complete files=%d bytes=%d read_errors=%d",
        image_snapshot.root.file_count,
        image_snapshot.root.total_bytes,
        image_snapshot.error_count,
    )
    logger.info("Telemetry runtime metadata collection started")
    return NonDatabaseTelemetrySnapshot(
        images=image_snapshot,
        git=_git_identity(),
        runtime=_runtime_reproducibility_metadata(),
    )


def collect_telemetry_payload(
    settings: TelemetrySettings,
    *,
    generated_at: datetime,
    trigger: str,
    conn=None,
    non_database_snapshot: NonDatabaseTelemetrySnapshot | None = None,
) -> dict[str, Any]:
    """Collect the aggregate summary attached to a synchronization operation."""
    window_end = _as_utc(generated_at)
    window_start = window_end - timedelta(days=TELEMETRY_WINDOW_DAYS)
    logger.info(
        "Telemetry summary collection started trigger=%s window_start=%s window_end=%s",
        trigger,
        _isoformat(window_start),
        _isoformat(window_end),
    )
    database = _collect_database_snapshot(
        window_end=window_end,
        expiration_seconds=settings.match_expiration_seconds,
        conn=conn,
    )
    logger.info("Telemetry database summary complete trigger=%s", trigger)
    if non_database_snapshot is None:
        non_database_snapshot = _collect_non_database_telemetry(settings)
    image_snapshot = non_database_snapshot.images
    git = non_database_snapshot.git
    runtime = non_database_snapshot.runtime
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
            "database": {
                "latest_applied_migration": database.pop("latest_applied_migration")
            },
            "runtime": runtime,
        },
    }
    if database:
        raise AssertionError(f"Unhandled telemetry database fields: {sorted(database)}")
    logger.info("Telemetry summary collection complete trigger=%s", trigger)
    return payload


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
        "subdirectory_count": snapshot.subdirectory_count,
        "subdirectories_truncated": snapshot.subdirectories_truncated,
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
