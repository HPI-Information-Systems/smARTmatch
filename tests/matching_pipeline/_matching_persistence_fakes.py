"""Shared fakes for matching persistence tests."""

from __future__ import annotations


class Cursor:
    """Context-manager cursor fake with SQL-dependent query results."""

    def __init__(
        self,
        *,
        auction_rows=(),
        lost_rows=(),
        finalized_row=(0, 0, 0),
        fail_text: str | None = None,
    ) -> None:
        self.auction_rows = list(auction_rows)
        self.lost_rows = list(lost_rows)
        self.finalized_row = finalized_row
        self.fail_text = fail_text
        self.execute_calls: list[tuple[str, object]] = []
        self.executemany_calls: list[tuple[str, list[tuple[object, ...]]]] = []
        self._fetchall_rows: list[tuple[object, object]] = []
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.exited = True
        return False

    def execute(self, sql, params=None):
        self.execute_calls.append((sql, params))
        if self.fail_text is not None and self.fail_text in sql:
            raise RuntimeError("synthetic cursor failure")
        if "SELECT image_file_id, auction_artwork_id" in sql:
            self._fetchall_rows = self.auction_rows
        elif "SELECT image_file_id, lost_artwork_id" in sql:
            self._fetchall_rows = self.lost_rows

    def executemany(self, sql, rows):
        materialized_rows = list(rows)
        self.executemany_calls.append((sql, materialized_rows))

    def fetchall(self):
        return list(self._fetchall_rows)

    def fetchone(self):
        return self.finalized_row


class Connection:
    """Connection fake that records transaction and resource handling."""

    def __init__(self, cursor: Cursor) -> None:
        self.cursor_instance = cursor
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0
        self.cursor_count = 0

    def cursor(self):
        self.cursor_count += 1
        return self.cursor_instance

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.close_count += 1
