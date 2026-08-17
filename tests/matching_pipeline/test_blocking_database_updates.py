"""Unit tests for image-blocking database updates."""

from __future__ import annotations

import unittest
from unittest import mock

from matching_pipeline.image_blocking import db_updates
from matching_pipeline.image_blocking.db_updates import ExpectedImageVersion
from tests.matching_pipeline._blocking_dino_db_test_support import FakeConnection


class DatabaseUpdateTests(unittest.TestCase):
    def test_version_coercion_and_conflicts(self) -> None:
        self.assertEqual(db_updates._coerce_image_versions([]), [])
        self.assertEqual(
            db_updates._coerce_image_versions(
                [
                    ExpectedImageVersion(" 2 ", 7),
                    ExpectedImageVersion(1, "8"),
                    ExpectedImageVersion("2", 7),
                ]
            ),
            [(2, 7), (1, 8)],
        )
        for value, message in (
            (ExpectedImageVersion(None, 1), "Missing image_file_id"),
            (ExpectedImageVersion("abc", 1), "image_file_id must be an integer"),
            (ExpectedImageVersion("0", 1), "image_file_id must be positive"),
            (ExpectedImageVersion("1", None), "Missing content_version"),
            (ExpectedImageVersion("1", "bad"), "content_version must be an integer"),
            (ExpectedImageVersion("1", 0), "content_version must be positive"),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    db_updates._coerce_image_versions([value])
        with self.assertRaisesRegex(ValueError, "Conflicting content versions"):
            db_updates._coerce_image_versions(
                [ExpectedImageVersion("2", 7), ExpectedImageVersion("2", 8)]
            )

    def test_mark_empty_versions_avoids_database(self) -> None:
        with mock.patch.object(db_updates, "connect_db") as connect:
            self.assertEqual(db_updates.mark_image_files_embedded([]), 0)
        connect.assert_not_called()

    def test_mark_versions_executes_conditional_update_and_commits(self) -> None:
        for rowcount, expected in ((3, 3), (-1, 0)):
            with self.subTest(rowcount=rowcount):
                connection = FakeConnection(rowcount)
                versions = [
                    ExpectedImageVersion("4", 10),
                    ExpectedImageVersion("4", 10),
                    ExpectedImageVersion("5", 11),
                ]
                with mock.patch.object(
                    db_updates, "connect_db", return_value=connection
                ), self.assertLogs(db_updates.logger, level="INFO") as logs:
                    updated = db_updates.mark_image_files_embedded(versions)
                self.assertEqual(updated, expected)
                self.assertTrue(connection.committed)
                self.assertIn("UPDATE image_file", connection.cursor_value.sql)
                self.assertIn(
                    "image.content_version = expected.content_version",
                    connection.cursor_value.sql,
                )
                self.assertEqual(
                    connection.cursor_value.params,
                    ([4, 5], [10, 11]),
                )
                self.assertIn(f"Marked {expected} of 2", logs.output[0])


if __name__ == "__main__":
    unittest.main()
