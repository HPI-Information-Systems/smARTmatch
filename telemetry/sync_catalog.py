"""Disk-backed advertised and requested synchronization catalog."""

import sqlite3
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from telemetry.sync_budget import WorkspaceBudget
from telemetry.sync_errors import SourceSnapshotChanged, SyncProtocolError
from telemetry.sync_utils import _canonical_uuid, _match_key


class SyncCatalog:
    """Disk-backed advertised/requested ID and hash catalog for one operation."""

    def __init__(
        self,
        path: Path,
        *,
        budget: WorkspaceBudget | None = None,
    ) -> None:
        self.path = path
        self._budget = budget
        if self._budget is not None:
            self._budget.track_file(path)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(
            """
            PRAGMA journal_mode = DELETE;
            PRAGMA synchronous = OFF;
            CREATE TABLE advertised_match (
                lost_id TEXT NOT NULL,
                auction_id TEXT NOT NULL,
                row_sha256 TEXT NOT NULL,
                PRIMARY KEY (lost_id, auction_id)
            ) WITHOUT ROWID;
            CREATE TABLE advertised_entity (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                row_sha256 TEXT NOT NULL,
                PRIMARY KEY (entity_type, entity_id)
            ) WITHOUT ROWID;
            CREATE TABLE requested_match (
                lost_id TEXT NOT NULL,
                auction_id TEXT NOT NULL,
                PRIMARY KEY (lost_id, auction_id)
            ) WITHOUT ROWID;
            CREATE INDEX requested_match_auction_id
                ON requested_match (auction_id);
            CREATE TABLE requested_entity (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                PRIMARY KEY (entity_type, entity_id)
            ) WITHOUT ROWID;
            """
        )
        self._conn.commit()
        self._refresh_workspace_usage()

    def _refresh_workspace_usage(self) -> None:
        if self._budget is not None:
            self._budget.refresh_file(self.path)

    def close(self) -> None:
        self._conn.close()

    def record_inventory(self, inventory: Mapping[str, Any]) -> None:
        matches = inventory.get("match_score") or {}
        lost = inventory.get("lost_artwork") or {}
        auction = inventory.get("auction_artwork") or {}
        match_values = []
        for item in matches.values():
            lost_id = _canonical_uuid(item["lost_id"])
            auction_id = _canonical_uuid(item["auction_id"])
            match_values.append((lost_id, auction_id, str(item["sha256"])))
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO advertised_match VALUES (?, ?, ?)",
                match_values,
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO advertised_entity VALUES ('lost_artwork', ?, ?)",
                [(_canonical_uuid(key), str(value)) for key, value in lost.items()],
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO advertised_entity VALUES ('auction_artwork', ?, ?)",
                [(_canonical_uuid(key), str(value)) for key, value in auction.items()],
            )
        self._refresh_workspace_usage()

    def record_needed(
        self,
        needed: Mapping[str, Any],
        *,
        page_inventory: Mapping[str, Any] | None,
    ) -> None:
        if page_inventory is not None:
            _validate_needed_acknowledgement(needed, page_inventory)
        match_pairs, lost_ids, auction_ids = _parse_needed_identifiers(needed)
        if page_inventory is None:
            self._validate_global_needed(match_pairs, lost_ids, auction_ids)
        with self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO requested_match VALUES (?, ?)",
                match_pairs,
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO requested_entity VALUES ('lost_artwork', ?)",
                [(value,) for value in lost_ids],
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO requested_entity VALUES ('auction_artwork', ?)",
                [(value,) for value in auction_ids],
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO requested_entity VALUES ('lost_artwork', ?)",
                [(lost_id,) for lost_id, _auction_id in match_pairs],
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO requested_entity VALUES ('auction_artwork', ?)",
                [(auction_id,) for _lost_id, auction_id in match_pairs],
            )
        self._refresh_workspace_usage()

    def _validate_global_needed(
        self,
        match_pairs: Sequence[tuple[str, str]],
        lost_ids: Sequence[str],
        auction_ids: Sequence[str],
    ) -> None:
        for lost_id, auction_id in match_pairs:
            if (
                self._conn.execute(
                    "SELECT 1 FROM advertised_match WHERE lost_id = ? AND auction_id = ?",
                    (lost_id, auction_id),
                ).fetchone()
                is None
            ):
                raise SyncProtocolError(
                    "Receiver requested match IDs outside the advertised inventory"
                )
        for entity_type, identifiers in (
            ("lost_artwork", lost_ids),
            ("auction_artwork", auction_ids),
        ):
            for entity_id in identifiers:
                if (
                    self._conn.execute(
                        "SELECT 1 FROM advertised_entity "
                        "WHERE entity_type = ? AND entity_id = ?",
                        (entity_type, entity_id),
                    ).fetchone()
                    is None
                ):
                    raise SyncProtocolError(
                        f"Receiver requested {entity_type} IDs outside the inventory"
                    )

    def requested_counts(self) -> tuple[int, int, int]:
        match_count = int(
            self._conn.execute("SELECT COUNT(*) FROM requested_match").fetchone()[0]
        )
        lost_count = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM requested_entity WHERE entity_type = 'lost_artwork'"
            ).fetchone()[0]
        )
        auction_count = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM requested_entity WHERE entity_type = 'auction_artwork'"
            ).fetchone()[0]
        )
        return match_count, lost_count, auction_count

    def iter_requested_matches(
        self, batch_size: int
    ) -> Iterator[list[tuple[str, str]]]:
        cursor = self._conn.execute(
            "SELECT lost_id, auction_id FROM requested_match "
            "ORDER BY lost_id, auction_id"
        )
        while rows := cursor.fetchmany(batch_size):
            yield [(str(row[0]), str(row[1])) for row in rows]

    def iter_extra_entities(
        self, entity_type: str, batch_size: int
    ) -> Iterator[list[str]]:
        if entity_type == "lost_artwork":
            join_column = "lost_id"
        elif entity_type == "auction_artwork":
            join_column = "auction_id"
        else:
            raise ValueError(f"Unsupported requested entity type: {entity_type}")
        cursor = self._conn.execute(
            f"""
            SELECT requested.entity_id
            FROM requested_entity requested
            WHERE requested.entity_type = ?
              AND NOT EXISTS (
                  SELECT 1 FROM requested_match matched
                  WHERE matched.{join_column} = requested.entity_id
              )
            ORDER BY requested.entity_id
            """,
            (entity_type,),
        )
        while rows := cursor.fetchmany(batch_size):
            yield [str(row[0]) for row in rows]

    def verify_content(
        self,
        content: Mapping[str, Any],
        *,
        expected_matches: set[tuple[str, str]],
        expected_lost: set[str],
        expected_auction: set[str],
    ) -> None:
        hashes = content.get("hashes") or {}
        actual_matches = {
            tuple(str(key).split(":", 1)) for key in (hashes.get("match_score") or {})
        }
        actual_lost = set((hashes.get("lost_artwork") or {}).keys())
        actual_auction = set((hashes.get("auction_artwork") or {}).keys())
        if (
            actual_matches != expected_matches
            or actual_lost != expected_lost
            or actual_auction != expected_auction
        ):
            raise SourceSnapshotChanged(
                "Requested telemetry rows changed or disappeared between snapshots"
            )
        for lost_id, auction_id in actual_matches:
            expected = self._conn.execute(
                "SELECT row_sha256 FROM advertised_match "
                "WHERE lost_id = ? AND auction_id = ?",
                (lost_id, auction_id),
            ).fetchone()
            actual = hashes["match_score"][_match_key(lost_id, auction_id)]
            if expected is None or str(expected[0]) != str(actual):
                raise SourceSnapshotChanged(
                    "Requested match changed between telemetry snapshots"
                )
        for entity_type, actual_hashes in (
            ("lost_artwork", hashes.get("lost_artwork") or {}),
            ("auction_artwork", hashes.get("auction_artwork") or {}),
        ):
            for entity_id, actual in actual_hashes.items():
                expected = self._conn.execute(
                    "SELECT row_sha256 FROM advertised_entity "
                    "WHERE entity_type = ? AND entity_id = ?",
                    (entity_type, entity_id),
                ).fetchone()
                if expected is None or str(expected[0]) != str(actual):
                    raise SourceSnapshotChanged(
                        f"Requested {entity_type} changed between telemetry snapshots"
                    )


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
    match_items = needed["match_score"] if "match_score" in needed else []
    if not isinstance(match_items, list) or any(
        not isinstance(row, Mapping) or "lost_id" not in row or "auction_id" not in row
        for row in match_items
    ):
        raise ValueError("Telemetry acknowledgement has invalid match IDs")
    requested_matches = {
        _match_key(str(row["lost_id"]), str(row["auction_id"])) for row in match_items
    }
    lost_items = needed["lost_artwork"] if "lost_artwork" in needed else []
    auction_items = needed["auction_artwork"] if "auction_artwork" in needed else []
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


def _parse_needed_identifiers(
    needed: Mapping[str, Any],
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    if not isinstance(needed, Mapping):
        raise SyncProtocolError(
            "Telemetry acknowledgement needed field must be an object"
        )
    allowed_fields = {"match_score", "lost_artwork", "auction_artwork"}
    if set(needed) - allowed_fields:
        raise SyncProtocolError(
            "Telemetry acknowledgement contains unknown needed fields"
        )
    match_items = needed["match_score"] if "match_score" in needed else []
    if not isinstance(match_items, list) or any(
        not isinstance(row, Mapping) or "lost_id" not in row or "auction_id" not in row
        for row in match_items
    ):
        raise SyncProtocolError("Telemetry acknowledgement has invalid match IDs")
    lost_items = needed["lost_artwork"] if "lost_artwork" in needed else []
    auction_items = needed["auction_artwork"] if "auction_artwork" in needed else []
    if not isinstance(lost_items, list) or not isinstance(auction_items, list):
        raise SyncProtocolError("Telemetry acknowledgement artwork IDs must be arrays")
    try:
        matches = sorted(
            {
                (_canonical_uuid(row["lost_id"]), _canonical_uuid(row["auction_id"]))
                for row in match_items
            }
        )
        lost = sorted({_canonical_uuid(value) for value in lost_items})
        auction = sorted({_canonical_uuid(value) for value in auction_items})
    except (TypeError, ValueError, AttributeError) as exc:
        raise SyncProtocolError(
            "Telemetry acknowledgement contains invalid UUIDs"
        ) from exc
    return matches, lost, auction
