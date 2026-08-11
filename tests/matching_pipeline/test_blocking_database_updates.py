"""Unit tests for image-blocking database updates."""

from __future__ import annotations

import unittest
from unittest import mock

from matching_pipeline.image_blocking import db_updates
from tests.matching_pipeline._blocking_dino_db_test_support import FakeConnection


class DatabaseUpdateTests(unittest.TestCase):
    def test_id_coercion(self) -> None:
        self.assertEqual(db_updates._coerce_image_file_ids([]), [])
        self.assertEqual(
            db_updates._coerce_image_file_ids([" 2 ", 1, "2"]), [2, 1]
        )
        for value, message in (
            (None, "Missing"),
            (" ", "Missing"),
            ("abc", "must be an integer"),
            (0, "Missing"),
            ("0", "must be positive"),
            (-2, "must be positive"),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    db_updates._coerce_image_file_ids([value])

    def test_mark_empty_ids_avoids_database(self) -> None:
        with mock.patch.object(db_updates, "connect_db") as connect:
            self.assertEqual(db_updates.mark_image_files_embedded([]), 0)
        connect.assert_not_called()

    def test_mark_ids_executes_update_commits_and_clamps_rowcount(self) -> None:
        for rowcount, expected in ((3, 3), (-1, 0)):
            with self.subTest(rowcount=rowcount):
                connection = FakeConnection(rowcount)
                with mock.patch.object(
                    db_updates, "connect_db", return_value=connection
                ), self.assertLogs(db_updates.logger, level="INFO") as logs:
                    updated = db_updates.mark_image_files_embedded(["4", "4", "5"])
                self.assertEqual(updated, expected)
                self.assertTrue(connection.committed)
                self.assertIn("UPDATE image_file", connection.cursor_value.sql)
                self.assertEqual(connection.cursor_value.params, ([4, 5],))
                self.assertIn(f"Marked {expected}", logs.output[0])


if __name__ == "__main__":
    unittest.main()
