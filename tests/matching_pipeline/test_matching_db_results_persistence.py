"""Tests for image-matching database result persistence."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from uuid import UUID

from _matching_persistence_fakes import Connection, Cursor
from matching_pipeline.image_matching import config, db_results, result_rows
from matching_pipeline.image_matching.results import AcceptedImageMatch, ImageMatchingRunResult

UUID_1 = UUID(int=1)
UUID_2 = UUID(int=2)
UUID_3 = UUID(int=3)
PROGRAM_ID = UUID(int=99)


class DbHelperTests(unittest.TestCase):
    def test_stable_program_id_is_deterministic_and_input_sensitive(self) -> None:
        first = db_results.stable_matching_program_id("matcher", "1")
        self.assertEqual(first, db_results.stable_matching_program_id("matcher", "1"))
        self.assertNotEqual(first, db_results.stable_matching_program_id("matcher", "2"))

    def test_ensure_program_executes_idempotent_upsert(self) -> None:
        cursor = Cursor()
        db_results._ensure_matching_program(cursor, PROGRAM_ID)
        sql, params = cursor.execute_calls[0]
        self.assertIn("INSERT INTO matching_program", sql)
        self.assertIn("ON CONFLICT (matching_program_id) DO UPDATE", sql)
        self.assertEqual(
            params,
            (
                PROGRAM_ID,
                config.MATCHING_PROGRAM_NAME,
                config.MATCHING_PROGRAM_VERSION,
                config.MATCHING_PROGRAM_DESCRIPTION,
            ),
        )

    def test_fetch_links_handles_empty_input_and_groups_rows(self) -> None:
        cursor = Cursor(auction_rows=[(10, UUID_1), (10, UUID_2), (11, UUID_3)])
        self.assertEqual(
            db_results._fetch_links(
                cursor, "auction_artwork_image_file", "auction_artwork_id", []
            ),
            {},
        )
        self.assertEqual(cursor.execute_calls, [])
        links = db_results._fetch_links(
            cursor, "auction_artwork_image_file", "auction_artwork_id", [10, 11]
        )
        self.assertEqual(links, {10: [UUID_1, UUID_2], 11: [UUID_3]})
        sql, params = cursor.execute_calls[0]
        self.assertIn("FROM auction_artwork_image_file", sql)
        self.assertEqual(params, ([10, 11],))

    def test_fetch_links_accepts_lost_table_and_rejects_unsafe_identifiers(self) -> None:
        cursor = Cursor(lost_rows=[("20", UUID_1)])
        self.assertEqual(
            db_results._fetch_links(
                cursor, "lost_artwork_image_file", "lost_artwork_id", [20]
            ),
            {20: [UUID_1]},
        )
        with self.assertRaisesRegex(ValueError, "Unsupported image link table"):
            db_results._fetch_links(cursor, "match_score", "lost_artwork_id", [20])
        with self.assertRaisesRegex(ValueError, "Unsupported artwork id column"):
            db_results._fetch_links(
                cursor, "lost_artwork_image_file", "matching_program_id", [20]
            )

    def test_require_links_accepts_complete_set_and_lists_missing_ids(self) -> None:
        db_results._require_links({1: [UUID_1], 2: [UUID_2]}, [2, 1, 2], "auction")
        with self.assertRaisesRegex(
            ValueError,
            r"No lost artwork links found for image_file_id values: \[2, 3\]",
        ):
            db_results._require_links({1: [UUID_1]}, [3, 1, 2, 3], "lost")

    def test_upsert_noops_for_empty_writes(self) -> None:
        cursor = Cursor()
        db_results._upsert_match_scores(cursor, PROGRAM_ID, [])
        self.assertEqual(cursor.executemany_calls, [])

    def test_upsert_serializes_all_write_values_and_tie_conditions(self) -> None:
        cursor = Cursor()
        write = result_rows.ImageMatchScoreWrite(
            lost_artwork_id=UUID_1,
            auction_artwork_id=UUID_2,
            image_matching_confidence=0.8,
            image_final_score=0.4,
            image_blocking_similarity=0.5,
            best_image_file_id=10,
            image_visualization={"z": 1, "a": {"value": 2}},
        )
        db_results._upsert_match_scores(cursor, PROGRAM_ID, [write])
        self.assertEqual(len(cursor.executemany_calls), 1)
        sql, rows = cursor.executemany_calls[0]
        self.assertIn("INSERT INTO match_score", sql)
        self.assertIn("ON CONFLICT (lost_id, auction_id) DO UPDATE", sql)
        self.assertIn("EXCLUDED.image_final_score", sql)
        self.assertIn("EXCLUDED.image_matching_confidence", sql)
        self.assertIn("EXCLUDED.image_blocking_similarity", sql)
        self.assertEqual(
            rows,
            [(
                UUID_1,
                UUID_2,
                0.8,
                0.4,
                0.5,
                PROGRAM_ID,
                json.dumps(write.image_visualization, sort_keys=True),
                10,
            )],
        )

    def test_finalize_coerces_null_and_numeric_counts(self) -> None:
        cursor = Cursor(finalized_row=(None, "2", 3))
        finalized = db_results._finalize_processed_auction_links(cursor, [10, 11])
        self.assertEqual(finalized.processed_auction_link_count, 0)
        self.assertEqual(finalized.processed_auction_artwork_count, 2)
        self.assertEqual(finalized.empty_auction_artwork_count, 3)
        sql, params = cursor.execute_calls[0]
        self.assertIn("UPDATE auction_artwork_image_file", sql)
        self.assertIn("UPDATE auction_artwork", sql)
        self.assertEqual(params, ([10, 11],))

    def test_merge_ids_preserves_first_occurrence_across_lists(self) -> None:
        self.assertEqual(
            db_results._merge_ids([2, 1, 2], [], [3, 1, 4]), [2, 1, 3, 4]
        )


class DbTransactionTests(unittest.TestCase):
    @staticmethod
    def _accepted_match() -> AcceptedImageMatch:
        return AcceptedImageMatch(
            "10", "auction.jpg", "20", "lost.jpg", 0.8, 0.5, {"matches": []}
        )

    def test_write_matching_run_delegates_structured_fields(self) -> None:
        match = self._accepted_match()
        run = ImageMatchingRunResult(["10"], [match], 1, 0, 0)
        sentinel = db_results.DbWriteResult(None, 1, 0, 1, 0, 0, 0)
        with patch.object(
            db_results, "write_image_matching_results_to_db", return_value=sentinel
        ) as write:
            self.assertIs(db_results.write_matching_run_to_db(run), sentinel)
        write.assert_called_once_with([match], ["10"])

    def test_successful_accepted_write_commits_all_operations(self) -> None:
        cursor = Cursor(
            auction_rows=[(10, UUID_1), (11, UUID_2)],
            lost_rows=[(20, UUID_3)],
            finalized_row=(2, 2, 1),
        )
        connection = Connection(cursor)
        with patch.object(db_results, "connect_db", return_value=connection):
            result = db_results.write_image_matching_results_to_db(
                [self._accepted_match()], ["10", "11", "10"]
            )
        expected_program = db_results.stable_matching_program_id(
            config.MATCHING_PROGRAM_NAME, config.MATCHING_PROGRAM_VERSION
        )
        self.assertEqual(
            result, db_results.DbWriteResult(expected_program, 1, 1, 2, 2, 2, 1)
        )
        self.assertEqual(connection.commit_count, 1)
        self.assertEqual(connection.rollback_count, 0)
        self.assertEqual(connection.close_count, 1)
        self.assertTrue(cursor.entered)
        self.assertTrue(cursor.exited)
        self.assertEqual(len(cursor.executemany_calls), 1)
        upsert_row = cursor.executemany_calls[0][1][0]
        self.assertEqual(upsert_row[0:5], (UUID_3, UUID_1, 0.8, 0.4, 0.5))
        self.assertEqual(upsert_row[5], expected_program)
        self.assertEqual(upsert_row[7], 10)

    def test_successful_empty_write_only_finalizes_and_commits(self) -> None:
        cursor = Cursor(finalized_row=(1, 1, 0))
        connection = Connection(cursor)
        with patch.object(db_results, "connect_db", return_value=connection):
            result = db_results.write_image_matching_results_to_db([], ["7", "7"])
        self.assertEqual(result, db_results.DbWriteResult(None, 0, 0, 1, 1, 1, 0))
        self.assertEqual(connection.commit_count, 1)
        self.assertEqual(connection.rollback_count, 0)
        self.assertEqual(connection.close_count, 1)
        self.assertEqual(cursor.executemany_calls, [])
        self.assertFalse(
            any("INSERT INTO matching_program" in sql for sql, _ in cursor.execute_calls)
        )

    def test_missing_accepted_auction_link_rolls_back_and_closes(self) -> None:
        cursor = Cursor(auction_rows=[])
        connection = Connection(cursor)
        with (
            patch.object(db_results, "connect_db", return_value=connection),
            self.assertRaisesRegex(
                ValueError,
                r"No auction artwork links found for image_file_id values: \[10\]",
            ),
        ):
            db_results.write_image_matching_results_to_db(
                [self._accepted_match()], ["10"]
            )
        self.assertEqual(connection.commit_count, 0)
        self.assertEqual(connection.rollback_count, 1)
        self.assertEqual(connection.close_count, 1)
        self.assertTrue(cursor.exited)

    def test_cursor_failure_rolls_back_and_closes(self) -> None:
        cursor = Cursor(fail_text="WITH input_ids")
        connection = Connection(cursor)
        with (
            patch.object(db_results, "connect_db", return_value=connection),
            self.assertRaisesRegex(RuntimeError, "synthetic cursor failure"),
        ):
            db_results.write_image_matching_results_to_db([], [])
        self.assertEqual(connection.commit_count, 0)
        self.assertEqual(connection.rollback_count, 1)
        self.assertEqual(connection.close_count, 1)

    def test_invalid_processed_id_fails_before_opening_connection(self) -> None:
        with (
            patch.object(db_results, "connect_db") as connect,
            self.assertRaisesRegex(ValueError, "auction_file_id must be positive"),
        ):
            db_results.write_image_matching_results_to_db([], ["0"])
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
