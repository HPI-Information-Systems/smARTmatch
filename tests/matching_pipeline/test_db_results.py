"""Tests for image-matching DB write SQL helpers."""

from __future__ import annotations

import unittest
from uuid import uuid4

from matching_pipeline.image_matching.db_results import (
    _finalize_processed_auction_links,
    _upsert_match_scores,
)
from matching_pipeline.image_matching.result_rows import ImageMatchScoreWrite


class DbResultSqlTests(unittest.TestCase):
    def test_upsert_writes_match_score_not_artwork_match(self) -> None:
        cursor = _FakeCursor()
        _upsert_match_scores(
            cursor,
            uuid4(),
            [
                ImageMatchScoreWrite(
                    lost_artwork_id=uuid4(),
                    auction_artwork_id=uuid4(),
                    image_matching_confidence=0.8,
                    image_final_score=0.4,
                    image_blocking_similarity=0.5,
                    best_image_file_id=10,
                    image_visualization={"image_matching": {"best_match": {}}},
                )
            ],
        )

        self.assertIn("INSERT INTO match_score", cursor.executemany_sql)
        self.assertIn("image_matching_confidence", cursor.executemany_sql)
        self.assertIn("best_image_file_id", cursor.executemany_sql)
        self.assertNotIn("image_sim", cursor.executemany_sql)
        self.assertNotIn("image_confidence_score", cursor.executemany_sql)
        self.assertIn("is_metadata_matching_processed = false", cursor.sql)
        self.assertEqual(len(cursor.rows), 1)
        self.assertEqual(cursor.rows[0][7], 10)

    def test_finalize_processed_auction_links_does_not_touch_image_file_flag(self) -> None:
        cursor = _FakeCursor(fetchone_result=(2, 1, 0))
        result = _finalize_processed_auction_links(cursor, [10, 11])

        self.assertIn("UPDATE auction_artwork_image_file", cursor.sql)
        self.assertIn("SET is_image_matching_processed = true", cursor.sql)
        self.assertIn("is_image_matching_completed_without_error = true", cursor.sql)
        self.assertNotIn("UPDATE image_file", cursor.sql)
        self.assertNotIn("img.is_image_matching_processed", cursor.sql)
        self.assertEqual(result.processed_auction_link_count, 2)
        self.assertEqual(result.processed_auction_artwork_count, 1)
        self.assertEqual(result.empty_auction_artwork_count, 0)


class _FakeCursor:
    def __init__(self, fetchone_result=None) -> None:
        self.sql = ""
        self.rows = []
        self.params = None
        self.executemany_sql = ""
        self.fetchone_result = fetchone_result

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def executemany(self, sql, rows):
        self.executemany_sql = sql
        self.rows = list(rows)

    def fetchone(self):
        return self.fetchone_result


if __name__ == "__main__":
    unittest.main()
