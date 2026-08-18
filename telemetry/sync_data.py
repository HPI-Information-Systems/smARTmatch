"""Requested data-page materialization, verification, and splitting."""

import gzip
from pathlib import Path
from typing import Any, Mapping, Sequence

from telemetry.sync_budget import (
    WorkspaceBudget,
    _ClosureMaterializationBudget,
)
from telemetry.sync_catalog import SyncCatalog
from telemetry.sync_codec import _write_raw_page
from telemetry.sync_constants import (
    _PAGE_ENVELOPE_RESERVE_BYTES,
    DATA_MATCHES_PER_PAGE,
    MAX_COMPRESSED_PAGE_BYTES,
    MAX_UNCOMPRESSED_PAGE_BYTES,
    TARGET_UNCOMPRESSED_PAGE_BYTES,
    logger,
)
from telemetry.sync_errors import (
    UnsendableClosureError,
    _ClosureMaterializationLimit,
)
from telemetry.sync_graph import _build_data_content
from telemetry.sync_models import RawPage
from telemetry.sync_queries import _fetch_requested_match_rows, _snapshot_connection
from telemetry.sync_utils import _canonical_json


def _spool_data_pages(
    directory: Path,
    *,
    catalog: SyncCatalog,
    budget: WorkspaceBudget,
    conn=None,
) -> list[RawPage]:
    directory.mkdir(parents=True, exist_ok=True)
    pages: list[RawPage] = []
    owns_connection = conn is None
    conn = conn or _snapshot_connection()
    match_total, lost_total, auction_total = catalog.requested_counts()
    try:
        matches_spooled = 0
        for batch in catalog.iter_requested_matches(DATA_MATCHES_PER_PAGE):
            _spool_requested_match_pairs(
                conn,
                directory,
                batch,
                pages,
                catalog=catalog,
                budget=budget,
            )
            matches_spooled += len(batch)
            logger.info(
                "Telemetry data spool progress matches=%d/%d pages=%d",
                matches_spooled,
                match_total,
                len(pages),
            )

        for entity_type, total in (
            ("lost_artwork", lost_total),
            ("auction_artwork", auction_total),
        ):
            spooled = 0
            for batch in catalog.iter_extra_entities(
                entity_type, DATA_MATCHES_PER_PAGE
            ):
                lost_batch = set(batch) if entity_type == "lost_artwork" else set()
                auction_batch = (
                    set(batch) if entity_type == "auction_artwork" else set()
                )
                _spool_data_content(
                    conn,
                    directory,
                    [],
                    lost_batch,
                    auction_batch,
                    pages,
                    catalog=catalog,
                    budget=budget,
                    expected_matches=set(),
                    expected_lost=lost_batch,
                    expected_auction=auction_batch,
                )
                spooled += len(batch)
                logger.info(
                    "Telemetry data spool progress entity_type=%s extra=%d "
                    "requested_total=%d pages=%d",
                    entity_type,
                    spooled,
                    total,
                    len(pages),
                )

        if not pages:
            budget.ensure_page_capacity()
            content = _build_data_content(conn, [], set(), set())
            catalog.verify_content(
                content,
                expected_matches=set(),
                expected_lost=set(),
                expected_auction=set(),
            )
            _write_raw_page(
                directory,
                content,
                pages,
                {"match_score": 0, "lost_artwork": 0, "auction_artwork": 0},
                budget=budget,
            )
        if owns_connection:
            conn.rollback()
    finally:
        if owns_connection:
            conn.close()
    return pages


def _spool_requested_match_pairs(
    conn,
    directory: Path,
    pairs: Sequence[tuple[str, str]],
    pages: list[RawPage],
    *,
    catalog: SyncCatalog,
    budget: WorkspaceBudget,
) -> None:
    if not pairs:
        return
    hard_limit = MAX_UNCOMPRESSED_PAGE_BYTES - _PAGE_ENVELOPE_RESERVE_BYTES
    requested_limit = TARGET_UNCOMPRESSED_PAGE_BYTES if len(pairs) > 1 else hard_limit
    materialization_limit = budget.next_page_materialization_limit(requested_limit)
    fetch_budget = _ClosureMaterializationBudget(materialization_limit)
    rows: list[dict[str, Any]] | None = None
    materialization_error: _ClosureMaterializationLimit | None = None
    try:
        rows = _fetch_requested_match_rows(
            conn,
            pairs,
            materialization_budget=fetch_budget,
        )
    except _ClosureMaterializationLimit as exc:
        materialization_error = exc

    if materialization_error is not None:
        if len(pairs) > 1:
            middle = len(pairs) // 2
            for half in (pairs[:middle], pairs[middle:]):
                _spool_requested_match_pairs(
                    conn,
                    directory,
                    half,
                    pages,
                    catalog=catalog,
                    budget=budget,
                )
            return
        raise UnsendableClosureError(
            "A requested match row cannot fit in one bounded data page "
            f"before closure loading ({materialization_error})"
        ) from None

    assert rows is not None
    expected_matches = set(pairs)
    _spool_data_content(
        conn,
        directory,
        rows,
        set(),
        set(),
        pages,
        catalog=catalog,
        budget=budget,
        expected_matches=expected_matches,
        expected_lost={lost_id for lost_id, _auction_id in expected_matches},
        expected_auction={auction_id for _lost_id, auction_id in expected_matches},
    )


def _spool_data_content(
    conn,
    directory: Path,
    match_rows: Sequence[Mapping[str, Any]],
    lost_ids: set[str],
    auction_ids: set[str],
    pages: list[RawPage],
    *,
    catalog: SyncCatalog,
    budget: WorkspaceBudget,
    expected_matches: set[tuple[str, str]],
    expected_lost: set[str],
    expected_auction: set[str],
) -> None:
    item_count = len(match_rows) + len(lost_ids) + len(auction_ids)
    hard_limit = MAX_UNCOMPRESSED_PAGE_BYTES - _PAGE_ENVELOPE_RESERVE_BYTES
    requested_limit = TARGET_UNCOMPRESSED_PAGE_BYTES if item_count > 1 else hard_limit
    materialization_limit = budget.next_page_materialization_limit(requested_limit)
    materialization_budget = _ClosureMaterializationBudget(materialization_limit)
    content: dict[str, Any] | None = None
    materialization_error: _ClosureMaterializationLimit | None = None
    try:
        if match_rows:
            match_bytes = len(_canonical_json(list(match_rows)))
            materialization_budget.reserve(
                match_bytes,
                len(match_rows),
                label="match_score rows",
            )
        content = _build_data_content(
            conn,
            match_rows,
            lost_ids,
            auction_ids,
            materialization_budget=materialization_budget,
        )
    except _ClosureMaterializationLimit as exc:
        materialization_error = exc

    if materialization_error is not None:
        if item_count > 1:
            _split_data_content(
                conn,
                directory,
                match_rows,
                lost_ids,
                auction_ids,
                pages,
                catalog=catalog,
                budget=budget,
            )
            return
        raise UnsendableClosureError(
            "A requested telemetry closure cannot fit in one bounded data page "
            f"before closure materialization ({materialization_error})"
        ) from None

    assert content is not None
    catalog.verify_content(
        content,
        expected_matches=expected_matches,
        expected_lost=expected_lost,
        expected_auction=expected_auction,
    )
    raw = _canonical_json(content)
    compressed_bytes = len(gzip.compress(raw, compresslevel=6, mtime=0))
    should_split = (
        len(raw) > TARGET_UNCOMPRESSED_PAGE_BYTES
        or compressed_bytes > MAX_COMPRESSED_PAGE_BYTES - _PAGE_ENVELOPE_RESERVE_BYTES
    )
    if should_split and item_count > 1:
        _split_data_content(
            conn,
            directory,
            match_rows,
            lost_ids,
            auction_ids,
            pages,
            catalog=catalog,
            budget=budget,
        )
        return
    if (
        len(raw) > hard_limit
        or compressed_bytes > MAX_COMPRESSED_PAGE_BYTES - _PAGE_ENVELOPE_RESERVE_BYTES
    ):
        raise UnsendableClosureError(
            "A requested telemetry closure cannot fit in one bounded data page "
            f"(uncompressed={len(raw)}, compressed={compressed_bytes})"
        )
    counts = {
        "match_score": len(content["rows"]["match_score"]),
        "lost_artwork": len(content["entities"]["lost_artwork"]),
        "auction_artwork": len(content["entities"]["auction_artwork"]),
    }
    _write_raw_page(
        directory,
        content,
        pages,
        counts,
        raw=raw,
        budget=budget,
    )


def _split_data_content(
    conn,
    directory: Path,
    match_rows: Sequence[Mapping[str, Any]],
    lost_ids: set[str],
    auction_ids: set[str],
    pages: list[RawPage],
    *,
    catalog: SyncCatalog,
    budget: WorkspaceBudget,
) -> None:
    if len(match_rows) > 1:
        middle = len(match_rows) // 2
        for half in (match_rows[:middle], match_rows[middle:]):
            half_matches = {
                (str(row["lost_id"]), str(row["auction_id"])) for row in half
            }
            _spool_data_content(
                conn,
                directory,
                half,
                set(),
                set(),
                pages,
                catalog=catalog,
                budget=budget,
                expected_matches=half_matches,
                expected_lost={lost_id for lost_id, _auction_id in half_matches},
                expected_auction={auction_id for _lost_id, auction_id in half_matches},
            )
        return

    combined = [("lost", value) for value in sorted(lost_ids)] + [
        ("auction", value) for value in sorted(auction_ids)
    ]
    middle = len(combined) // 2
    for half in (combined[:middle], combined[middle:]):
        half_lost = {value for kind, value in half if kind == "lost"}
        half_auction = {value for kind, value in half if kind == "auction"}
        _spool_data_content(
            conn,
            directory,
            [],
            half_lost,
            half_auction,
            pages,
            catalog=catalog,
            budget=budget,
            expected_matches=set(),
            expected_lost=half_lost,
            expected_auction=half_auction,
        )
