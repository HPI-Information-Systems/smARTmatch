"""End-to-end two-snapshot selective synchronization operation."""

import json
import shutil
import time
from datetime import datetime
from typing import Any, Callable, Mapping
from uuid import uuid4

from telemetry.sync_budget import TransferBudget, WorkspaceBudget
from telemetry.sync_catalog import SyncCatalog
from telemetry.sync_codec import (
    _check_transfer_budget,
    _operation_hash,
    _page_envelope,
    _preflight_phase_pages,
    encode_sync_page,
)
from telemetry.sync_constants import logger
from telemetry.sync_data import _spool_data_pages
from telemetry.sync_http import (
    _post_page_with_retries,
    _sleep_before_next_page,
    _wait_for_async_apply,
)
from telemetry.sync_inventory import _spool_inventory_pages
from telemetry.sync_models import EncodedSyncPage, SyncDeliveryResult, SyncSettings
from telemetry.sync_queries import _snapshot_connection
from telemetry.sync_utils import _as_utc, _isoformat
from telemetry.sync_workspace import sync_workspace


def deliver_sync_operation(
    settings: SyncSettings,
    *,
    trigger: str,
    generated_at: datetime,
    summary: Mapping[str, Any] | None = None,
    summary_factory: Callable[[Any], Mapping[str, Any]] | None = None,
) -> SyncDeliveryResult:
    """Run a bounded, two-snapshot inventory and requested-data sync."""
    generated_at = _as_utc(generated_at)
    sync_id = str(uuid4())
    started_at = time.monotonic()
    total_compressed_bytes = 0
    transfer_budget = TransferBudget()
    logger.info(
        "Telemetry sync started sync_id=%s trigger=%s generated_at=%s",
        sync_id,
        trigger,
        _isoformat(generated_at),
    )

    with sync_workspace() as root:
        budget = WorkspaceBudget(root)
        catalog = SyncCatalog(root / "catalog.sqlite3", budget=budget)
        try:
            logger.info("Telemetry sync inventory spooling started sync_id=%s", sync_id)
            inventory_conn = _snapshot_connection()
            try:
                if summary_factory is not None:
                    summary = dict(summary_factory(inventory_conn))
                else:
                    summary = dict(summary or {})
                inventory_pages = _spool_inventory_pages(
                    root / "inventory",
                    conn=inventory_conn,
                    catalog=catalog,
                    budget=budget,
                )
                inventory_conn.rollback()
            finally:
                inventory_conn.close()
            logger.info(
                "Telemetry sync inventory spooling complete sync_id=%s pages=%d matches=%d",
                sync_id,
                len(inventory_pages),
                sum(page.counts.get("match_score", 0) for page in inventory_pages),
            )
            inventory_transfer_bytes = _preflight_phase_pages(
                sync_id=sync_id,
                trigger=trigger,
                generated_at=generated_at,
                phase="inventory",
                pages=inventory_pages,
                summary=summary,
            )
            _check_transfer_budget(inventory_transfer_bytes)

            # No PostgreSQL snapshot is held during receiver negotiation.
            for page_number, raw_page in enumerate(inventory_pages):
                content = json.loads(raw_page.path.read_text(encoding="utf-8"))
                catalog.record_inventory(content["inventory"])
                envelope = _page_envelope(
                    sync_id=sync_id,
                    trigger=trigger,
                    generated_at=generated_at,
                    phase="inventory",
                    page_number=page_number,
                    pages=inventory_pages,
                    content=content,
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
                    transfer_budget=transfer_budget,
                )
                catalog.record_needed(
                    acknowledgement["needed"],
                    page_inventory=(
                        None
                        if page_number == len(inventory_pages) - 1
                        else content["inventory"]
                    ),
                )
                budget.ensure_within_limit()
                total_compressed_bytes += len(encoded.body)
                requested_counts = catalog.requested_counts()
                logger.info(
                    "Telemetry sync progress sync_id=%s phase=inventory page=%d/%d "
                    "status=acknowledged requested_matches=%d requested_lost=%d "
                    "requested_auction=%d",
                    sync_id,
                    page_number + 1,
                    len(inventory_pages),
                    *requested_counts,
                )
                _sleep_before_next_page(settings)

            shutil.rmtree(root / "inventory")
            budget.release(sum(page.workspace_bytes for page in inventory_pages))
            requested_counts = catalog.requested_counts()
            logger.info(
                "Telemetry sync data spooling started sync_id=%s matches=%d lost=%d "
                "auction=%d",
                sync_id,
                *requested_counts,
            )
            data_conn = _snapshot_connection()
            try:
                data_pages = _spool_data_pages(
                    root / "data",
                    catalog=catalog,
                    budget=budget,
                    conn=data_conn,
                )
                data_conn.rollback()
            finally:
                data_conn.close()
            logger.info(
                "Telemetry sync data spooling complete sync_id=%s pages=%d",
                sync_id,
                len(data_pages),
            )

            operation_hash = _operation_hash(data_pages)
            data_transfer_bytes = _preflight_phase_pages(
                sync_id=sync_id,
                trigger=trigger,
                generated_at=generated_at,
                phase="data",
                pages=data_pages,
                summary=None,
                operation_hash=operation_hash,
            )
            _check_transfer_budget(inventory_transfer_bytes + data_transfer_bytes)
            last_encoded: EncodedSyncPage | None = None
            final_acknowledgement: dict[str, Any] | None = None
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
                acknowledgement = _post_page_with_retries(
                    settings,
                    encoded,
                    sync_id=sync_id,
                    phase="data",
                    page_number=page_number,
                    page_count=len(data_pages),
                    transfer_budget=transfer_budget,
                )
                total_compressed_bytes += len(encoded.body)
                last_encoded = encoded
                if page_number + 1 == len(data_pages):
                    final_acknowledgement = acknowledgement
                logger.info(
                    "Telemetry sync progress sync_id=%s phase=data page=%d/%d "
                    "status=acknowledged",
                    sync_id,
                    page_number + 1,
                    len(data_pages),
                )
                if page_number + 1 < len(data_pages):
                    _sleep_before_next_page(settings)
        finally:
            catalog.close()

    assert last_encoded is not None
    if final_acknowledgement and final_acknowledgement.get("accepted") is True:
        _wait_for_async_apply(
            settings,
            sync_id=sync_id,
            operation_sha256=operation_hash,
        )
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
