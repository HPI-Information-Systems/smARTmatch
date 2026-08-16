"""Safety and orchestration tests for physical auction-image cleanup."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from matching_pipeline.image_cleanup import __main__ as entrypoint
from matching_pipeline.image_cleanup import cleanup
from shared.image_storage_lock import image_storage_lock


class _Cursor:
    def __init__(
        self,
        rows,
        *,
        lock_acquired: bool = True,
        active_scraper: bool = False,
        mark_result_ids: list[int] | None = None,
    ) -> None:
        self.rows = list(rows)
        self.lock_acquired = lock_acquired
        self.active_scraper = active_scraper
        self.mark_result_ids = mark_result_ids
        self.execute_calls: list[tuple[str, object]] = []
        self.marked_image_file_ids: list[int] = []
        self._last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, sql, params=None) -> None:
        self._last_sql = str(sql)
        self.execute_calls.append((self._last_sql, params))
        if "UPDATE image_file" in self._last_sql:
            self.marked_image_file_ids = list(params[0])

    def fetchone(self):
        if "pg_try_advisory_xact_lock" in self._last_sql:
            return (self.lock_acquired,)
        assert "FROM scraper_run" in self._last_sql
        return (self.active_scraper,)

    def fetchall(self):
        if "UPDATE image_file" in self._last_sql:
            image_file_ids = (
                self.marked_image_file_ids
                if self.mark_result_ids is None
                else self.mark_result_ids
            )
            return [(image_file_id,) for image_file_id in image_file_ids]
        assert "WITH eligible_artwork" in self._last_sql
        return list(self.rows)


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self.cursor_instance = cursor
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.close_count += 1


def _row(
    image_file_id: int,
    path: Path | str,
    *,
    candidate: bool = False,
    lost: bool = False,
    protected_auction: bool = False,
    score_reference: bool = False,
):
    return (
        image_file_id,
        str(path),
        candidate,
        lost,
        protected_auction,
        score_reference,
    )


def _run(
    rows,
    root: Path,
    *,
    apply: bool,
    lock_acquired: bool = True,
    active_scraper: bool = False,
    mark_result_ids: list[int] | None = None,
):
    cursor = _Cursor(
        rows,
        lock_acquired=lock_acquired,
        active_scraper=active_scraper,
        mark_result_ids=mark_result_ids,
    )
    connection = _Connection(cursor)
    with mock.patch.object(cleanup, "connect_db", return_value=connection):
        result = cleanup.cleanup_unmatched_auction_images(
            image_root=root,
            apply=apply,
        )
    return result, connection, cursor


def test_candidate_sql_requires_complete_processing_and_no_match_score():
    sql = " ".join(cleanup._INVENTORY_SQL.split())

    assert "aa.is_image_matching_processed = true" in sql
    assert "aa.is_metadata_extraction_processed = true" in sql
    assert "aa.is_metadata_matching_processed = true" in sql
    assert "pending.is_image_matching_processed = false" in sql
    assert "pending.is_image_matching_completed_without_error = false" in sql
    assert "linked_image.is_embedded = false" in sql
    assert "score.auction_id = aa.auction_artwork_id" in sql
    assert "img.cleaned_up_at IS NULL" in sql
    assert "img.file_path IS NOT NULL" in sql
    assert "metadata_final_score" not in sql
    assert "image_final_score" not in sql
    assert "rating" not in sql


def test_apply_deletes_and_marks_only_unmatched_image_rows():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        unmatched = root / "unmatched.jpg"
        matched = root / "matched.jpg"
        lost = root / "lost.jpg"
        shared_incomplete = root / "shared-incomplete.jpg"
        for path, payload in (
            (unmatched, b"unmatched"),
            (matched, b"matched"),
            (lost, b"lost"),
            (shared_incomplete, b"incomplete"),
        ):
            path.write_bytes(payload)

        rows = [
            _row(1, "unmatched.jpg", candidate=True),
            # A separate path alias belonging to a matched artwork protects the target.
            _row(2, "matched.jpg", candidate=True),
            _row(3, matched, protected_auction=True),
            # Lost-artwork aliases always win, even when an auction row is eligible.
            _row(4, "lost.jpg", candidate=True),
            _row(5, lost, lost=True),
            # One shared image_file row can have eligible and incomplete owners.
            _row(
                6,
                shared_incomplete,
                candidate=True,
                protected_auction=True,
            ),
        ]
        result, connection, cursor = _run(rows, root, apply=True)

        assert not unmatched.exists()
        assert matched.read_bytes() == b"matched"
        assert lost.read_bytes() == b"lost"
        assert shared_incomplete.read_bytes() == b"incomplete"
        assert result.deleted_target_count == 1
        assert result.protected_target_count == 3
        assert result.byte_count == len(b"unmatched")
        assert result.cleaned_image_file_ids == (1,)
        assert result.cleaned_image_row_count == 1
        assert not result.has_failures
        assert connection.commit_count == 1
        assert connection.rollback_count == 0
        assert connection.close_count == 1

        sql_statements = [sql for sql, _params in cursor.execute_calls]
        assert any("LOCK TABLE scraper_run" in sql for sql in sql_statements)
        assert any("idle_in_transaction_session_timeout = 0" in sql for sql in sql_statements)
        cleanup_sql = next(sql for sql in sql_statements if "UPDATE image_file" in sql)
        assert "SET file_path = NULL" in cleanup_sql
        assert "cleaned_up_at = now()" in cleanup_sql
        assert cursor.marked_image_file_ids == [1]
        assert not any(
            re.search(r"\b(?:INSERT|DELETE|TRUNCATE|ALTER|DROP)\b", sql, re.I)
            for sql in sql_statements
        )


def test_any_direct_match_score_reference_protects_all_path_aliases():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        path = root / "scored.jpg"
        path.write_bytes(b"score")
        rows = [
            _row(1, "scored.jpg", candidate=True),
            _row(2, path, score_reference=True),
        ]

        result, _connection, _cursor = _run(rows, root, apply=True)

        assert path.read_bytes() == b"score"
        assert result.protected_target_count == 1
        assert result.deleted_target_count == 0
        assert result.cleaned_image_row_count == 0


def test_duplicate_candidate_rows_delete_one_physical_target_once():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        path = root / "duplicate.jpg"
        path.write_bytes(b"duplicate")
        rows = [
            _row(1, "duplicate.jpg", candidate=True),
            _row(2, path, candidate=True),
        ]

        result, _connection, _cursor = _run(rows, root, apply=True)

        assert not path.exists()
        assert result.candidate_image_row_count == 2
        assert result.candidate_target_count == 1
        assert result.deleted_target_count == 1
        assert result.cleaned_image_file_ids == (1, 2)


def test_stored_filename_whitespace_is_preserved_exactly():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        spaced = root / "candidate.jpg "
        unspaced = root / "candidate.jpg"
        spaced.write_bytes(b"delete-spaced")
        unspaced.write_bytes(b"keep-unspaced")

        result, _connection, _cursor = _run(
            [_row(1, "candidate.jpg ", candidate=True)],
            root,
            apply=True,
        )

        assert not spaced.exists()
        assert unspaced.read_bytes() == b"keep-unspaced"
        assert result.deleted_target_count == 1
        assert result.cleaned_image_file_ids == (1,)


def test_dry_run_reports_bytes_without_unlinking_or_taking_write_locks():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        path = root / "preview.jpg"
        path.write_bytes(b"preview")

        result, connection, cursor = _run(
            [_row(1, path, candidate=True)],
            root,
            apply=False,
        )

        assert path.read_bytes() == b"preview"
        assert not (root / ".smartmatch-image-storage.lock").exists()
        assert result.would_delete_target_count == 1
        assert result.deleted_target_count == 0
        assert result.byte_count == len(b"preview")
        assert result.cleaned_image_row_count == 0
        assert connection.commit_count == 0
        assert connection.rollback_count == 1
        assert any("READ ONLY" in sql for sql, _params in cursor.execute_calls)
        assert not any("LOCK TABLE" in sql for sql, _params in cursor.execute_calls)


def test_storage_coordination_lock_file_can_never_be_deleted():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        with image_storage_lock(root, exclusive=False):
            pass

        result, _connection, _cursor = _run(
            [_row(1, ".smartmatch-image-storage.lock", candidate=True)],
            root,
            apply=True,
        )

        assert (root / ".smartmatch-image-storage.lock").is_file()
        assert result.unsafe_target_count == 1
        assert result.deleted_target_count == 0


def test_missing_files_are_idempotent_success_in_apply_mode():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        with image_storage_lock(root, exclusive=False):
            pass
        result, _connection, _cursor = _run(
            [_row(1, "already-gone.jpg", candidate=True)],
            root,
            apply=True,
        )

        assert result.missing_target_count == 1
        assert result.deleted_target_count == 0
        assert result.cleaned_image_file_ids == (1,)
        assert not result.has_failures
        assert _connection.commit_count == 1
        assert _cursor.marked_image_file_ids == [1]


def test_apply_refuses_an_absent_or_uninitialized_image_root():
    with tempfile.TemporaryDirectory() as tmp_dir:
        absent = Path(tmp_dir) / "absent"
        with mock.patch.object(cleanup, "connect_db") as connect, pytest.raises(
            FileNotFoundError,
            match="image storage root does not exist",
        ):
            cleanup.cleanup_unmatched_auction_images(image_root=absent, apply=True)
        connect.assert_not_called()

        empty = Path(tmp_dir) / "empty"
        empty.mkdir()
        with mock.patch.object(cleanup, "connect_db") as connect, pytest.raises(
            RuntimeError,
            match="root is empty",
        ):
            cleanup.cleanup_unmatched_auction_images(image_root=empty, apply=True)
        connect.assert_not_called()
        assert list(empty.iterdir()) == []


def test_root_disappearing_after_validation_rolls_back_without_marking_rows():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        path = root / "candidate.jpg"
        path.write_bytes(b"keep")
        cursor = _Cursor([_row(1, path, candidate=True)])
        connection = _Connection(cursor)

        with mock.patch.object(
            cleanup, "connect_db", return_value=connection
        ), mock.patch.object(
            cleanup, "_open_image_root", side_effect=FileNotFoundError
        ), pytest.raises(
            RuntimeError,
            match="root disappeared",
        ):
            cleanup.cleanup_unmatched_auction_images(image_root=root, apply=True)

        assert path.read_bytes() == b"keep"
        assert cursor.marked_image_file_ids == []
        assert connection.commit_count == 0
        assert connection.rollback_count == 1
        assert connection.close_count == 1


def test_database_marker_mismatch_rolls_back_and_fails_loudly():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        path = root / "candidate.jpg"
        path.write_bytes(b"delete")
        cursor = _Cursor(
            [_row(1, path, candidate=True)],
            mark_result_ids=[],
        )
        connection = _Connection(cursor)

        with mock.patch.object(cleanup, "connect_db", return_value=connection), pytest.raises(
            RuntimeError,
            match="Could not mark all cleaned image_file rows",
        ):
            cleanup.cleanup_unmatched_auction_images(image_root=root, apply=True)

        assert not path.exists()
        assert connection.commit_count == 0
        assert connection.rollback_count == 1
        assert connection.close_count == 1


def test_out_of_root_symlink_and_directory_targets_are_rejected():
    with tempfile.TemporaryDirectory() as tmp_dir, tempfile.TemporaryDirectory() as outside_dir:
        root = Path(tmp_dir)
        outside = Path(outside_dir) / "outside.jpg"
        outside.write_bytes(b"outside")
        symlink = root / "alias.jpg"
        symlink.symlink_to(outside)
        directory = root / "not-a-file"
        directory.mkdir()

        result, _connection, _cursor = _run(
            [
                _row(1, outside, candidate=True),
                _row(2, symlink, candidate=True),
                _row(3, directory, candidate=True),
            ],
            root,
            apply=True,
        )

        assert outside.read_bytes() == b"outside"
        assert symlink.is_symlink()
        assert directory.is_dir()
        # The symlink and absolute path resolve to the same outside target, so
        # target-scoped accounting reports one unsafe alias group plus the directory.
        assert result.unsafe_target_count == 2
        assert result.candidate_target_count == 2
        assert result.deleted_target_count == 0
        assert result.has_failures


def test_symlink_to_an_in_root_file_is_rejected_without_deleting_target():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        target = root / "target.jpg"
        target.write_bytes(b"target")
        alias = root / "alias.jpg"
        alias.symlink_to(target.name)

        result, _connection, _cursor = _run(
            [_row(1, alias, candidate=True)],
            root,
            apply=True,
        )

        assert alias.is_symlink()
        assert target.read_bytes() == b"target"
        assert result.unsafe_target_count == 1
        assert result.deleted_target_count == 0


def test_unlink_failure_is_reported_and_file_remains():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        path = root / "permission.jpg"
        path.write_bytes(b"keep")
        with mock.patch.object(cleanup.os, "unlink", side_effect=PermissionError("denied")):
            result, _connection, _cursor = _run(
                [_row(1, path, candidate=True)],
                root,
                apply=True,
            )

        assert path.read_bytes() == b"keep"
        assert result.failed_target_count == 1
        assert result.cleaned_image_row_count == 0
        assert result.has_failures
        assert "denied" in result.errors[0]


def test_cleanup_rejects_a_root_that_differs_from_configured_image_storage():
    with tempfile.TemporaryDirectory() as tmp_dir:
        configured = Path(tmp_dir) / "images"
        ancestor = Path(tmp_dir)
        with mock.patch.dict(
            "os.environ",
            {"SMARTMATCH_IMAGES_DIR": str(configured)},
            clear=False,
        ), mock.patch.object(cleanup, "connect_db") as connect, pytest.raises(
            ValueError,
            match="must equal SMARTMATCH_IMAGES_DIR",
        ):
            cleanup.cleanup_unmatched_auction_images(
                image_root=ancestor,
                apply=True,
            )
        connect.assert_not_called()


def test_apply_skips_before_connecting_when_a_coordinated_writer_holds_storage_lock():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        path = root / "candidate.jpg"
        path.write_bytes(b"keep")

        with image_storage_lock(root, exclusive=False), mock.patch.object(
            cleanup, "connect_db"
        ) as connect, pytest.raises(cleanup.CleanupBlockedByImageWriter):
            cleanup.cleanup_unmatched_auction_images(image_root=root, apply=True)

        assert path.read_bytes() == b"keep"
        connect.assert_not_called()


def test_apply_skips_before_inventory_when_a_tracked_scraper_is_running():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        path = root / "candidate.jpg"
        path.write_bytes(b"keep")
        cursor = _Cursor(
            [_row(1, path, candidate=True)],
            active_scraper=True,
        )
        connection = _Connection(cursor)

        with mock.patch.object(cleanup, "connect_db", return_value=connection), pytest.raises(
            cleanup.CleanupBlockedByActiveScraper
        ):
            cleanup.cleanup_unmatched_auction_images(image_root=root, apply=True)

        assert path.read_bytes() == b"keep"
        assert connection.rollback_count == 1
        assert connection.close_count == 1
        assert not any("WITH eligible_artwork" in sql for sql, _params in cursor.execute_calls)


def test_apply_fails_closed_when_advisory_lock_is_held():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        path = root / "candidate.jpg"
        path.write_bytes(b"keep")
        cursor = _Cursor([_row(1, path, candidate=True)], lock_acquired=False)
        connection = _Connection(cursor)

        with mock.patch.object(cleanup, "connect_db", return_value=connection), pytest.raises(
            cleanup.CleanupAlreadyRunning
        ):
            cleanup.cleanup_unmatched_auction_images(image_root=root, apply=True)

        assert path.read_bytes() == b"keep"
        assert connection.rollback_count == 1
        assert connection.close_count == 1
        assert not any("WITH eligible_artwork" in sql for sql, _params in cursor.execute_calls)


def test_fresh_schema_and_model_define_cleanup_state():
    root = Path(__file__).resolve().parents[2]
    schema = (root / "db/init-production/01_schema_production.sql").read_text()
    model = (root / "scrapers/models_production.py").read_text()

    assert "is_image_matching_completed_without_error boolean NOT NULL DEFAULT false" in schema
    assert "is_image_matching_completed_without_error: Mapped[bool]" in model
    assert "NEW.title" in schema
    assert "NEW.description IS DISTINCT FROM OLD.description" in schema
    assert "NEW.is_metadata_matching_processed = false" in schema
    assert "cleaned_up_at timestamptz" in schema
    assert "cleaned_up_at: Mapped[Optional[datetime]]" in model


def test_entrypoint_is_dry_run_by_default_and_apply_errors_are_nonzero():
    success = mock.Mock(has_failures=False)
    success.apply = False
    for field in (
        "inventory_row_count",
        "candidate_image_row_count",
        "candidate_target_count",
        "protected_target_count",
        "would_delete_target_count",
        "deleted_target_count",
        "missing_target_count",
        "unsafe_target_count",
        "failed_target_count",
        "byte_count",
        "cleaned_image_row_count",
    ):
        setattr(success, field, 0)

    with mock.patch.object(entrypoint, "configure_logging"), mock.patch.object(
        entrypoint, "env_image_root", return_value=Path("/images")
    ), mock.patch.object(
        entrypoint, "cleanup_unmatched_auction_images", return_value=success
    ) as run:
        assert entrypoint.main([]) == 0
    run.assert_called_once_with(image_root=Path("/images"), apply=False)

    failed = mock.Mock(**vars(success))
    failed.has_failures = True
    with mock.patch.object(entrypoint, "configure_logging"), mock.patch.object(
        entrypoint, "cleanup_unmatched_auction_images", return_value=failed
    ) as run:
        assert entrypoint.main(["--apply", "--images-dir", "/safe/images"]) == 1
    run.assert_called_once_with(image_root=Path("/safe/images"), apply=True)


def test_entrypoint_treats_active_scraper_as_a_safe_skip():
    with mock.patch.object(entrypoint, "configure_logging"), mock.patch.object(
        entrypoint, "env_image_root", return_value=Path("/images")
    ), mock.patch.object(
        entrypoint,
        "cleanup_unmatched_auction_images",
        side_effect=cleanup.CleanupBlockedByActiveScraper,
    ):
        assert entrypoint.main(["--apply"]) == 0

    with mock.patch.object(entrypoint, "configure_logging"), mock.patch.object(
        entrypoint, "env_image_root", return_value=Path("/images")
    ), mock.patch.object(
        entrypoint,
        "cleanup_unmatched_auction_images",
        side_effect=cleanup.CleanupBlockedByImageWriter,
    ):
        assert entrypoint.main(["--apply"]) == 0
