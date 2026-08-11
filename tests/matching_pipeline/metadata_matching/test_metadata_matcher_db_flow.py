# run all matching test scripts with: python -m pytest -q tests/matching

"""
 This test focuses on the database flow of the metadata matcher, using a fake in-memory database to assert that:
 - The matcher processes all lost x auction pairs and persists only those above the threshold.
 - The inserted rows have expected structure and values.
 - All auction artworks are marked as processed.
 - The matcher fails fast if the matching program is missing.
 """

from __future__ import annotations

import pytest
from dataclasses import dataclass, field
from typing import Any
from conftest import auction_db_row, load_fixture_csv, lost_db_row

POSITIVE_MATCH_IDS = {"match100", "match101", "match102", "match103", "match104", "match105"}


@dataclass
class FakeDatabase:
    lost_rows: list[tuple[Any, ...]]
    auction_rows: list[tuple[Any, ...]]
    program_exists: bool = True
    inserted_rows: list[tuple[Any, ...]] = field(default_factory=list)
    processed_auction_ids: list[str] = field(default_factory=list)
    commits: int = 0
    executed_sql: list[tuple[str, Any]] = field(default_factory=list)


class FakeCursor:
    def __init__(self, db: FakeDatabase, name: str | None = None):
        self.db = db
        self.name = name
        self._rows: list[tuple[Any, ...]] = []
        self._fetch_index = 0
        self._one: tuple[Any, ...] | None = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def execute(self, sql: str, params: Any = None):
        normalized = " ".join(sql.lower().split())
        self.db.executed_sql.append((normalized, params))

        if "from matching_program" in normalized:
            self._one = (1,) if self.db.program_exists else None
        elif "from lost_artwork l" in normalized:
            self._rows = self.db.lost_rows
        elif "from auction_artwork c" in normalized:
            self._rows = self.db.auction_rows
            self._fetch_index = 0
        elif "update auction_artwork" in normalized and "is_metadata_matching_processed" in normalized:
            self.db.processed_auction_ids.extend(list(params[0]))
        else:
            # Technique/material enrichment is allowed to query variant tables in tests.
            self._rows = []
            self._one = None

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]):
        self.db.executed_sql.append(("executemany match_score", len(rows)))
        self.db.inserted_rows.extend(list(rows))

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows

    def fetchmany(self, size: int):
        start = self._fetch_index
        end = min(start + size, len(self._rows))
        self._fetch_index = end
        return self._rows[start:end]

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, db: FakeDatabase):
        self.db = db
        self.closed = False

    def cursor(self, name: str | None = None):
        return FakeCursor(self.db, name)

    def commit(self):
        self.db.commits += 1

    def close(self):
        self.closed = True


class FakeDbConnect:
    def __init__(self, db: FakeDatabase):
        self.db = db
        self.connections: list[FakeConnection] = []

    def connect(self, *args, **kwargs):
        conn = FakeConnection(self.db)
        self.connections.append(conn)
        return conn


def test_match_metadata_processes_csv_rows_and_persists_pair_scores(monkeypatch):
    from matching_pipeline.metadata_matching.main_matcher import metadata_matcher as matcher

    lost_rows = [lost_db_row(row) for row in load_fixture_csv("10_lostart.csv")]
    auction_rows = [auction_db_row(row) for row in load_fixture_csv("10_spsg.csv")]
    db = FakeDatabase(lost_rows=lost_rows, auction_rows=auction_rows)
    fake_db_connect = FakeDbConnect(db)

    monkeypatch.setattr(matcher, "db_connect", fake_db_connect.connect)
    monkeypatch.setattr(matcher, "_BATCH_SIZE", 4)
    monkeypatch.setattr(matcher, "_score_dimensions", lambda *_args: 1.0)

    # Confidence functions use production database/cache state. Keep this test focused
    # on matching flow and final score aggregation; similarity functions remain real.
    monkeypatch.setattr(matcher, "title_confidence_function", lambda lost, auction: 1.0 if lost and auction else 0.0)
    monkeypatch.setattr(matcher, "artist_confidence_function", lambda lost, auction: 1.0 if lost and auction else 0.0)
    monkeypatch.setattr(matcher, "dating_confidence_function", lambda *args: 1.0 if all(arg is not None for arg in args) else 0.0)
    monkeypatch.setattr(matcher, "dimensions_confidence_function", lambda *args: 1.0 if all(arg is not None for arg in args) else 0.0)
    monkeypatch.setattr(matcher, "technique_material_confidence_function", lambda *args: (1.0, 1.0))

    # Run the matching program with the fake database and assert on interactions and results.
    result = matcher.match_metadata()
    assert result["lost_loaded"] == len(lost_rows)
    assert result["auction_pairs_processed"] == len(lost_rows) * len(auction_rows)

    # match_metadata evaluates every lost x auction pair, but persists only rows that pass matcher.THRESHOLD.
    # Do not assert that every evaluated pair is inserted unless the test explicitly monkeypatches THRESHOLD = 0.0.
    assert 0 < len(db.inserted_rows) <= len(lost_rows) * len(auction_rows)

    # row[8] = final_score, which must be above the threshold for all inserted rows.
    assert all(row[8] >= matcher.THRESHOLD for row in db.inserted_rows)

    # Assert that all auction artworks were marked as processed.
    assert set(db.processed_auction_ids) == {row[0] for row in auction_rows}

    # Assert that all database connections were properly initialized and closed.
    assert len(fake_db_connect.connections) == 3
    assert all(conn.closed for conn in fake_db_connect.connections)

    # Assert that inserted rows have expected structure and values.
    for row in db.inserted_rows:
        assert len(row) == 11
        assert row[10] == matcher.MATCHING_PROGRAM_ID
        assert 0.0 <= row[8] <= 1.0
        assert 0.0 <= row[9] <= 1.0

    # Assert that known positive matches have high scores.
    auction_id_to_match_id = {
        row["spsg_artwork_id"]: row["lost_artwork_id"] for row in load_fixture_csv("10_spsg.csv")
    }
    rows_by_lost_id: dict[str, list[tuple[Any, ...]]] = {}
    for row in db.inserted_rows:
        rows_by_lost_id.setdefault(row[0], []).append(row)

    # For each known positive match, assert that at least one of the inserted rows for that lost_artwork_id has a final_score above 0.90.
    for match_id in POSITIVE_MATCH_IDS:
        best = max(rows_by_lost_id[match_id], key=lambda row: row[8])
        # Assert that the best scoring row for this lost_artwork_id corresponds to the correct auction_artwork_id and has a high final_score.
        assert auction_id_to_match_id[best[1]] == match_id
        assert best[8] >= 0.90


def test_match_metadata_fails_fast_when_matching_program_is_missing(monkeypatch):
    from matching_pipeline.metadata_matching.main_matcher import metadata_matcher as matcher

    db = FakeDatabase(lost_rows=[], auction_rows=[], program_exists=False)
    fake_db_connect = FakeDbConnect(db)
    monkeypatch.setattr(matcher, "db_connect", fake_db_connect.connect)

    with pytest.raises(RuntimeError, match="matching_program_id"):
        matcher.match_metadata()

    assert db.inserted_rows == []
    assert db.processed_auction_ids == []
    assert all(conn.closed for conn in fake_db_connect.connections)
