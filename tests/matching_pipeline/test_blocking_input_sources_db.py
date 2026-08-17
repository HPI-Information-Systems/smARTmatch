"""Offline unit coverage for blocking database input helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _blocking_test_support import Connection, Cursor

from matching_pipeline.image_blocking import input_sources
from matching_pipeline.image_blocking.config import AUCTION_ROLE, LOST_ROLE
from matching_pipeline.image_blocking.input_sources import ImageFileRow


class InputSourceDatabaseTests(unittest.TestCase):
    def test_processed_replay_reset_is_atomic_and_marks_links_pending(self) -> None:
        class ReplayCursor:
            def __init__(self) -> None:
                self.executions = []
                self.rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, sql, params=None):
                self.executions.append((str(sql), params))
                self.rowcount = 5 if len(self.executions) == 1 else 2

        class ReplayConnection:
            def __init__(self) -> None:
                self.cursor_instance = ReplayCursor()
                self.commits = 0
                self.rollbacks = 0
                self.closes = 0

            def cursor(self):
                return self.cursor_instance

            def commit(self):
                self.commits += 1

            def rollback(self):
                self.rollbacks += 1

            def close(self):
                self.closes += 1

        connection = ReplayConnection()
        with mock.patch.object(input_sources, "connect_db", return_value=connection):
            self.assertEqual(
                input_sources.reset_auction_image_matching_for_replay(),
                (5, 2),
            )

        sql = "\n".join(call[0] for call in connection.cursor_instance.executions)
        self.assertIn("is_image_matching_processed = false", sql)
        self.assertIn("is_image_matching_completed_without_error = false", sql)
        self.assertIn("is_image_matching_processed_at = NULL", sql)
        self.assertIn("image.cleaned_up_at IS NULL", sql)
        self.assertIn("image.file_path IS NOT NULL", sql)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        self.assertEqual(connection.closes, 1)

    def test_db_presence_and_schema_checks(self) -> None:
        for value in (True, False):
            conn = Connection([Cursor(one=(value,))])
            with mock.patch.object(input_sources, "connect_db", return_value=conn):
                self.assertIs(
                    input_sources.has_unprocessed_auction_image_file_rows(), value
                )
            self.assertIn("SELECT EXISTS", conn.used[0].executions[0][0])
            self.assertIn("img.is_embedded = false", conn.used[0].executions[0][0])

        input_sources._require_image_file_path_column(
            Connection([Cursor(one=(True,))])
        )
        with self.assertRaisesRegex(RuntimeError, "content_version.*migration 24"):
            input_sources._require_image_file_path_column(
                Connection([Cursor(one=(False,))])
            )

    def test_load_db_rows_coordinates_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            conn = Connection(
                [
                    Cursor(one=(True,)),
                    Cursor(rows=[("lost", "lost.jpg", True, 3, "a" * 64)]),
                    Cursor(rows=[("auction", "auction.jpg", False, 4, "b" * 64)]),
                ]
            )
            with mock.patch.object(
                input_sources, "connect_db", return_value=conn
            ), mock.patch.object(input_sources, "default_image_root", return_value=root):
                lost, auction = input_sources.load_db_image_file_rows(
                    lost_limit=2,
                    auction_limit=3,
                    include_processed_auction_images=True,
                    validate_files=False,
                )

            self.assertEqual(
                lost,
                [
                    ImageFileRow(
                        "lost",
                        root / "lost.jpg",
                        is_embedded=True,
                        content_version=3,
                        content_sha256="a" * 64,
                    )
                ],
            )
            self.assertEqual(
                auction,
                [
                    ImageFileRow(
                        "auction",
                        root / "auction.jpg",
                        is_embedded=False,
                        content_version=4,
                        content_sha256="b" * 64,
                    )
                ],
            )
            self.assertEqual(conn.used[1].executions[0][1], (2,))
            self.assertEqual(conn.used[2].executions[0][1], (3,))
            self.assertIn("WITH selected_auction_artwork", conn.used[2].executions[0][0])

    def test_fetch_rows_limits_resolution_and_bad_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image_root = Path(tmp).resolve()
            existing = image_root / "exists.jpg"
            existing.write_bytes(b"x")

            with mock.patch.object(
                input_sources, "default_image_root", return_value=image_root
            ):
                rows = input_sources._fetch_db_rows(
                    Connection([Cursor(rows=[("id", "exists.jpg")])]),
                    LOST_ROLE,
                    None,
                    False,
                    True,
                )
                self.assertEqual(rows[0].file_path, existing)

                with self.assertRaisesRegex(ValueError, "limits must be positive"):
                    input_sources._fetch_db_rows(
                        Connection([]), LOST_ROLE, 0, False, False
                    )
                with self.assertRaisesRegex(ValueError, "Missing image_file.file_path"):
                    input_sources._fetch_db_rows(
                        Connection([Cursor(rows=[("id", None)])]),
                        LOST_ROLE,
                        None,
                        False,
                        False,
                    )
                with self.assertRaisesRegex(FileNotFoundError, "Image file not found"):
                    input_sources._fetch_db_rows(
                        Connection([Cursor(rows=[("id", "gone.jpg")])]),
                        LOST_ROLE,
                        None,
                        False,
                        True,
                    )

    def test_query_variants_and_invalid_role(self) -> None:
        lost = input_sources._db_query(LOST_ROLE, False)
        self.assertIn("lost_artwork_image_file", lost)
        self.assertIn("img.cleaned_up_at IS NULL", lost)
        self.assertIn("img.file_path IS NOT NULL", lost)
        self.assertIn("img.is_embedded", lost)
        self.assertIn("img.content_version", lost)
        self.assertIn("img.content_sha256", lost)

        unprocessed = input_sources._db_query(AUCTION_ROLE, False)
        processed = input_sources._db_query(AUCTION_ROLE, True)
        self.assertIn("img.is_embedded = false", unprocessed)
        self.assertIn("is_image_matching_processed = false", unprocessed)
        self.assertIn("is_image_matching_completed_without_error = false", unprocessed)
        self.assertIn("FROM match_score score", unprocessed)
        self.assertIn("img.cleaned_up_at IS NULL", unprocessed)
        self.assertIn("img.file_path IS NOT NULL", unprocessed)
        self.assertIn("img.is_embedded", unprocessed)
        self.assertIn("img.content_version", unprocessed)
        self.assertIn("img.content_sha256", unprocessed)
        self.assertNotIn("is_image_matching_processed = false", processed)
        self.assertNotIn("is_image_matching_completed_without_error", processed)

        limited_unprocessed = input_sources._auction_image_file_query(
            False, use_auction_artwork_limit=True
        )
        limited_processed = input_sources._auction_image_file_query(
            True, use_auction_artwork_limit=True
        )
        self.assertIn("WITH selected_auction_artwork", limited_unprocessed)
        self.assertIn("selected_image.is_embedded = false", limited_unprocessed)
        self.assertIn("img.is_embedded = false", limited_unprocessed)
        self.assertIn("aaif.is_image_matching_processed = false", limited_unprocessed)
        self.assertIn(
            "aaif.is_image_matching_completed_without_error = false",
            limited_unprocessed,
        )
        self.assertNotIn("is_image_matching_processed", limited_processed)

        with self.assertRaisesRegex(ValueError, "Unsupported role"):
            input_sources._db_query("invalid", False)
        with self.assertRaisesRegex(ValueError, "Unsupported image SQL alias"):
            input_sources._auction_image_state_filter(False, image_alias="unsafe")

    def test_db_path_validation_and_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            absolute = root / "absolute.jpg"
            absolute.write_bytes(b"x")
            self.assertEqual(
                input_sources._db_image_file_path(
                    root,
                    image_file_id="1",
                    raw_file_path=f"  {absolute}  ",
                    validate_files=True,
                ),
                absolute,
            )
            self.assertEqual(
                input_sources._resolve_db_file_path(root, absolute, False), absolute
            )
            with self.assertRaisesRegex(ValueError, "Missing image_file.file_path"):
                input_sources._db_image_file_path(
                    root,
                    image_file_id="1",
                    raw_file_path=" ",
                    validate_files=False,
                )
            with self.assertRaisesRegex(FileNotFoundError, "Image file not found"):
                input_sources._db_image_file_path(
                    root,
                    image_file_id="1",
                    raw_file_path=root / "missing.jpg",
                    validate_files=True,
                )

    def test_relative_db_candidates_cover_order_fallback_and_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp).resolve()
            repo = outer / "repo"
            image_root = repo / "db" / "images"
            image_root.mkdir(parents=True)
            repo_file = repo / "db" / "images" / "repo.jpg"
            repo_file.write_bytes(b"x")
            image_file = image_root / "plain.jpg"
            image_file.write_bytes(b"x")

            with mock.patch.object(input_sources, "repo_root", return_value=repo):
                prefixed = input_sources._db_file_path_candidates(
                    image_root, Path("db/images/repo.jpg")
                )
                self.assertEqual(prefixed[0], repo_file)
                self.assertEqual(
                    input_sources._resolve_db_file_path(
                        image_root, "db/images/repo.jpg", True
                    ),
                    repo_file,
                )
                self.assertEqual(
                    input_sources._resolve_db_file_path(image_root, "plain.jpg", True),
                    image_file,
                )
                fallback = input_sources._resolve_db_file_path(
                    image_root, "not-there.jpg", False
                )
                self.assertEqual(fallback, image_root / "not-there.jpg")

                self.assertTrue(
                    input_sources._path_starts_with(
                        Path("db/images/a.jpg"), Path("db/images")
                    )
                )
                self.assertFalse(
                    input_sources._path_starts_with(Path("other/a.jpg"), Path("db"))
                )
                self.assertEqual(
                    input_sources._unique_paths([image_root, image_root / "."]),
                    [image_root],
                )

            outside_root = outer / "outside"
            outside_root.mkdir()
            with mock.patch.object(input_sources, "repo_root", return_value=repo):
                outside_candidates = input_sources._db_file_path_candidates(
                    outside_root, Path("relative.jpg")
                )
            self.assertEqual(outside_candidates[0], outside_root / "relative.jpg")


if __name__ == "__main__":
    unittest.main()
