"""Canonical page envelopes, encoding, and spool-file writes."""

import gzip
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from telemetry.sync_budget import WorkspaceBudget
from telemetry.sync_constants import (
    MAX_COMPRESSED_PAGE_BYTES,
    MAX_SYNC_TRANSFER_BYTES,
    MAX_UNCOMPRESSED_PAGE_BYTES,
    SYNC_SCHEMA_VERSION,
)
from telemetry.sync_errors import (
    SyncWorkspaceLimitError,
    UnsendableClosureError,
)
from telemetry.sync_models import EncodedSyncPage, RawPage
from telemetry.sync_utils import _canonical_json, _isoformat


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
            }
            if final
            else None
        ),
    }


def _preflight_phase_pages(
    *,
    sync_id: str,
    trigger: str,
    generated_at: datetime,
    phase: str,
    pages: Sequence[RawPage],
    summary: Mapping[str, Any] | None,
    operation_hash: str | None = None,
) -> int:
    """Encode every page before phase delivery and return compressed bytes."""
    total_compressed_bytes = 0
    for page_number, raw_page in enumerate(pages):
        envelope = _page_envelope(
            sync_id=sync_id,
            trigger=trigger,
            generated_at=generated_at,
            phase=phase,
            page_number=page_number,
            pages=pages,
            content=json.loads(raw_page.path.read_text(encoding="utf-8")),
            summary=(
                dict(summary) if summary is not None and page_number == 0 else None
            ),
            operation_hash=operation_hash,
        )
        try:
            encoded = encode_sync_page(envelope)
            total_compressed_bytes += len(encoded.body)
        except ValueError as exc:
            raise UnsendableClosureError(
                f"Telemetry {phase} page {page_number + 1}/{len(pages)} "
                f"cannot be delivered: {exc}"
            ) from exc
    return total_compressed_bytes


def _check_transfer_budget(total_compressed_bytes: int) -> None:
    if total_compressed_bytes > MAX_SYNC_TRANSFER_BYTES:
        raise SyncWorkspaceLimitError(
            "Telemetry sync transfer would exceed "
            f"{MAX_SYNC_TRANSFER_BYTES} compressed bytes "
            f"(planned={total_compressed_bytes})"
        )


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


def _write_raw_page(
    directory: Path,
    content: Mapping[str, Any],
    pages: list[RawPage],
    counts: Mapping[str, int],
    *,
    raw: bytes | None = None,
    budget: WorkspaceBudget | None = None,
) -> None:
    raw = raw if raw is not None else _canonical_json(content)
    if budget is not None:
        budget.reserve_page(len(raw))
    path = directory / f"page-{len(pages):08d}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)
    pages.append(
        RawPage(
            path=path,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            counts=dict(counts),
            workspace_bytes=len(raw),
        )
    )


def _inventory_counts(content: Mapping[str, Any]) -> dict[str, int]:
    inventory = content["inventory"]
    return {key: len(value) for key, value in inventory.items()}


def _operation_hash(pages: Sequence[RawPage]) -> str:
    digest = hashlib.sha256(b"smartmatch-sync-operation-v3\0")
    for page in pages:
        digest.update(page.content_sha256.encode("ascii"))
    return digest.hexdigest()
