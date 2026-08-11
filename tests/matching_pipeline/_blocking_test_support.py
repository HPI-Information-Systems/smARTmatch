"""Shared fakes and directory patches for blocking pipeline unit tests."""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest import mock

from matching_pipeline.image_blocking import pipeline


class Cursor:
    def __init__(self, *, one=None, rows=()) -> None:
        self.one = one
        self.rows = list(rows)
        self.executions: list[tuple[str, tuple[int, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=()):
        self.executions.append((sql, params))

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, cursors) -> None:
        self.cursors = list(cursors)
        self.used: list[Cursor] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        cursor = self.cursors.pop(0)
        self.used.append(cursor)
        return cursor


@contextlib.contextmanager
def patched_dirs(root: Path, candidates: Path):
    with mock.patch.object(pipeline, "blocking_root", return_value=root), mock.patch.object(
        pipeline, "candidate_dir", return_value=candidates
    ), mock.patch.object(
        pipeline, "lost_embedding_cache_path", return_value=root / "lost" / "embeddings.npz"
    ):
        yield
