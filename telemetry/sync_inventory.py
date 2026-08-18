"""Full-history inventory pagination and graph-hash spooling."""

from pathlib import Path
from typing import Any, Sequence

from telemetry.sync_budget import (
    WorkspaceBudget,
    _ClosureMaterializationBudget,
)
from telemetry.sync_catalog import SyncCatalog
from telemetry.sync_codec import _inventory_counts, _write_raw_page
from telemetry.sync_constants import (
    _PAGE_ENVELOPE_RESERVE_BYTES,
    DATA_MATCHES_PER_PAGE,
    INVENTORY_MATCHES_PER_PAGE,
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
from telemetry.sync_utils import _match_key


def _spool_inventory_pages(
    directory: Path,
    *,
    conn=None,
    catalog: SyncCatalog | None = None,
    budget: WorkspaceBudget | None = None,
) -> list[RawPage]:
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
            if budget is not None:
                budget.ensure_page_capacity(reserved_pages=1)
            inventory: dict[str, Any] = {
                "match_score": {},
                "lost_artwork": {},
                "auction_artwork": {},
            }
            pairs = [(str(row["lost_id"]), str(row["auction_id"])) for row in rows]
            for start in range(0, len(pairs), DATA_MATCHES_PER_PAGE):
                _add_inventory_hashes(
                    conn,
                    pairs[start : start + DATA_MATCHES_PER_PAGE],
                    inventory,
                    workspace_budget=budget,
                )
            content = {"inventory": inventory}
            if catalog is not None:
                catalog.record_inventory(content["inventory"])
            _write_raw_page(
                directory,
                content,
                pages,
                _inventory_counts(content),
                budget=budget,
            )
            matches_spooled += len(rows)
            logger.info(
                "Telemetry inventory spool progress pages=%d matches=%d",
                len(pages),
                matches_spooled,
            )
            last = rows[-1]
            cursor = (str(last["lost_id"]), str(last["auction_id"]))
        if not pages:
            if budget is not None:
                budget.ensure_page_capacity(reserved_pages=1)
            content = {
                "inventory": {
                    "match_score": {},
                    "lost_artwork": {},
                    "auction_artwork": {},
                }
            }
            if catalog is not None:
                catalog.record_inventory(content["inventory"])
            _write_raw_page(
                directory,
                content,
                pages,
                _inventory_counts(content),
                budget=budget,
            )
        if owns_connection:
            conn.rollback()
    finally:
        if owns_connection:
            conn.close()
    return pages


def _add_inventory_hashes(
    conn,
    pairs: Sequence[tuple[str, str]],
    inventory: dict[str, Any],
    *,
    workspace_budget: WorkspaceBudget | None = None,
) -> None:
    if not pairs:
        return
    hard_limit = MAX_UNCOMPRESSED_PAGE_BYTES - _PAGE_ENVELOPE_RESERVE_BYTES
    requested_limit = TARGET_UNCOMPRESSED_PAGE_BYTES if len(pairs) > 1 else hard_limit
    materialization_limit = (
        workspace_budget.next_page_materialization_limit(requested_limit)
        if workspace_budget is not None
        else requested_limit
    )
    materialization_budget = _ClosureMaterializationBudget(materialization_limit)
    data_content: dict[str, Any] | None = None
    materialization_error: _ClosureMaterializationLimit | None = None
    try:
        match_rows = _fetch_requested_match_rows(
            conn,
            pairs,
            materialization_budget=materialization_budget,
        )
        data_content = _build_data_content(
            conn,
            match_rows,
            set(),
            set(),
            materialization_budget=materialization_budget,
        )
    except _ClosureMaterializationLimit as exc:
        materialization_error = exc

    if materialization_error is not None:
        if len(pairs) > 1:
            middle = len(pairs) // 2
            for half in (pairs[:middle], pairs[middle:]):
                _add_inventory_hashes(
                    conn,
                    half,
                    inventory,
                    workspace_budget=workspace_budget,
                )
            return
        raise UnsendableClosureError(
            "A telemetry inventory closure cannot fit in one bounded page "
            f"before closure materialization ({materialization_error})"
        ) from None

    assert data_content is not None
    graph_hashes = data_content["hashes"]
    for lost_id, auction_id in pairs:
        key = _match_key(lost_id, auction_id)
        inventory["match_score"][key] = {
            "lost_id": lost_id,
            "auction_id": auction_id,
            "sha256": graph_hashes["match_score"][key],
        }
        inventory["lost_artwork"][lost_id] = graph_hashes["lost_artwork"][lost_id]
        inventory["auction_artwork"][auction_id] = graph_hashes["auction_artwork"][
            auction_id
        ]


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
                ms.auction_id
            FROM match_score ms
            {where}
            ORDER BY ms.lost_id, ms.auction_id
            LIMIT %s
            """,
            params,
        )
        return [{"lost_id": row[0], "auction_id": row[1]} for row in cur.fetchall()]
