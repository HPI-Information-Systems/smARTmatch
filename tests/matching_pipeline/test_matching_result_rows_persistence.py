"""Tests for image-matching persistence row preparation."""

from __future__ import annotations

import unittest
from uuid import UUID

from matching_pipeline.image_matching import result_rows
from matching_pipeline.image_matching.results import AcceptedImageMatch


UUID_1 = UUID(int=1)
UUID_2 = UUID(int=2)
UUID_3 = UUID(int=3)
UUID_4 = UUID(int=4)
PROGRAM_ID = UUID(int=99)


class ResultRowValidationTests(unittest.TestCase):
    def test_coerce_image_ids_preserves_order_and_removes_duplicates(self) -> None:
        self.assertEqual(
            result_rows.coerce_image_file_ids([" 2 ", "1", "2", 3], "file"),
            [2, 1, 3],
        )

    def test_coerce_image_id_rejects_missing_non_integer_and_non_positive(self) -> None:
        cases = (
            (None, "Missing sample_id"),
            ("  ", "Missing sample_id"),
            ("abc", "sample_id must be an integer image_file_id: 'abc'"),
            ("1.5", "sample_id must be an integer image_file_id: '1.5'"),
            ("0", "sample_id must be positive: 0"),
            ("-3", "sample_id must be positive: -3"),
        )
        for value, message in cases:
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, message
            ):
                result_rows._coerce_image_file_id(value, "sample_id")

    def test_score_coercion_accepts_boundaries_and_numeric_strings(self) -> None:
        self.assertEqual(result_rows._coerce_score(0, "confidence"), 0.0)
        self.assertEqual(result_rows._coerce_score("0.25", "confidence"), 0.25)
        self.assertEqual(result_rows._coerce_score(1, "confidence"), 1.0)

    def test_score_coercion_rejects_types_and_out_of_range_values(self) -> None:
        for value, message in (
            (None, "Invalid confidence: None"),
            ("bad", "Invalid confidence: 'bad'"),
            (-0.01, "confidence must be in \\[0, 1\\]: -0.01"),
            (1.01, "confidence must be in \\[0, 1\\]: 1.01"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, message
            ):
                result_rows._coerce_score(value, "confidence")

    def test_blocking_score_coercion_handles_none_boundaries_and_strings(self) -> None:
        self.assertIsNone(result_rows._coerce_blocking_score(None))
        self.assertEqual(result_rows._coerce_blocking_score(-1), -1.0)
        self.assertEqual(result_rows._coerce_blocking_score("0.5"), 0.5)
        self.assertEqual(result_rows._coerce_blocking_score(1), 1.0)

    def test_blocking_score_coercion_rejects_types_and_out_of_range_values(self) -> None:
        for value, message in (
            (object(), "Invalid image blocking similarity"),
            ("bad", "Invalid image blocking similarity: 'bad'"),
            (-1.01, "Image blocking similarity must be in \\[-1, 1\\]: -1.01"),
            (1.01, "Image blocking similarity must be in \\[-1, 1\\]: 1.01"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, message
            ):
                result_rows._coerce_blocking_score(value)

    def test_final_score_uses_confidence_or_confidence_times_blocking(self) -> None:
        self.assertEqual(result_rows._image_final_score(0.8, None), 0.8)
        self.assertAlmostEqual(result_rows._image_final_score(0.8, 0.5), 0.4)

    def test_required_links_returns_values_and_reports_role(self) -> None:
        links = {10: [UUID_1]}
        self.assertIs(result_rows._required_links(links, 10, "auction"), links[10])
        with self.assertRaisesRegex(
            ValueError,
            "No lost artwork link found for image_file_id=20",
        ):
            result_rows._required_links({}, 20, "lost")


class ScorePreparationTests(unittest.TestCase):
    def test_empty_matches_produce_no_writes(self) -> None:
        self.assertEqual(
            result_rows.prepare_match_score_image_writes(
                [],
                auction_links={},
                lost_links={},
                matching_program_id=PROGRAM_ID,
            ),
            [],
        )

    def test_preparation_fans_out_sorts_and_builds_visualization(self) -> None:
        match = AcceptedImageMatch(
            "10", "auction.jpg", "20", "lost.jpg", "0.8", None  # type: ignore[arg-type]
        )
        writes = result_rows.prepare_match_score_image_writes(
            [match],
            auction_links={10: [UUID_2, UUID_1]},
            lost_links={20: [UUID_4, UUID_3]},
            matching_program_id=PROGRAM_ID,
        )

        pairs = [(row.lost_artwork_id, row.auction_artwork_id) for row in writes]
        expected = [(lost, auction) for lost in (UUID_4, UUID_3) for auction in (UUID_2, UUID_1)]
        self.assertEqual(pairs, sorted(expected, key=lambda pair: (str(pair[0]), str(pair[1]))))
        for row in writes:
            self.assertEqual(row.image_matching_confidence, 0.8)
            self.assertEqual(row.image_final_score, 0.8)
            self.assertIsNone(row.image_blocking_similarity)
            self.assertEqual(row.best_image_file_id, 10)
            best = row.image_visualization["image_matching"]["best_match"]
            self.assertEqual(best["auction_image_file_id"], 10)
            self.assertEqual(best["lost_image_file_id"], 20)
            self.assertEqual(best["auction_file_path"], "auction.jpg")
            self.assertEqual(best["lost_file_path"], "lost.jpg")
            self.assertEqual(best["image_final_score"], 0.8)
            self.assertNotIn("keypoint_matches", best)
            self.assertEqual(
                row.image_visualization["image_matching"]["matching_program_id"],
                str(PROGRAM_ID),
            )

    def test_preparation_selects_better_match_and_includes_keypoints(self) -> None:
        keypoints = {"coordinate_space": "image_pixels", "matches": [{"score": 1.0}]}
        writes = result_rows.prepare_match_score_image_writes(
            [
                AcceptedImageMatch("10", "old.jpg", "20", "lost.jpg", 0.5, 0.5),
                AcceptedImageMatch(
                    "11", "best.jpg", "20", "lost.jpg", 0.8, 0.75, keypoints
                ),
                AcceptedImageMatch("10", "worse.jpg", "20", "lost.jpg", 0.1, 0.1),
            ],
            auction_links={10: [UUID_1], 11: [UUID_1]},
            lost_links={20: [UUID_2]},
            matching_program_id=PROGRAM_ID,
        )

        self.assertEqual(len(writes), 1)
        row = writes[0]
        self.assertEqual(row.best_image_file_id, 11)
        self.assertEqual(row.image_matching_confidence, 0.8)
        self.assertEqual(row.image_blocking_similarity, 0.75)
        self.assertAlmostEqual(row.image_final_score, 0.6)
        self.assertEqual(
            row.image_visualization["image_matching"]["best_match"]["keypoint_matches"],
            keypoints,
        )

    def test_preparation_rejects_missing_auction_and_lost_links(self) -> None:
        match = AcceptedImageMatch("10", "a.jpg", "20", "l.jpg", 0.5)
        with self.assertRaisesRegex(ValueError, "No auction artwork link"):
            result_rows.prepare_match_score_image_writes(
                [match],
                auction_links={},
                lost_links={20: [UUID_1]},
                matching_program_id=PROGRAM_ID,
            )
        with self.assertRaisesRegex(ValueError, "No lost artwork link"):
            result_rows.prepare_match_score_image_writes(
                [match],
                auction_links={10: [UUID_1]},
                lost_links={},
                matching_program_id=PROGRAM_ID,
            )

    def test_tie_breaker_covers_every_ordering_case(self) -> None:
        current = {"final_score": 0.8, "confidence": 0.7, "blocking_score": 0.4}
        self.assertTrue(result_rows._is_better(0.9, 0.1, None, current))
        self.assertFalse(result_rows._is_better(0.7, 1.0, 1.0, current))
        self.assertTrue(result_rows._is_better(0.8, 0.8, None, current))
        self.assertFalse(result_rows._is_better(0.8, 0.6, 1.0, current))
        self.assertFalse(result_rows._is_better(0.8, 0.7, None, current))
        self.assertTrue(result_rows._is_better(0.8, 0.7, 0.5, current))
        self.assertFalse(result_rows._is_better(0.8, 0.7, 0.4, current))

        no_current_blocking = {
            "final_score": 0.8,
            "confidence": 0.7,
            "blocking_score": None,
        }
        self.assertTrue(result_rows._is_better(0.8, 0.7, 0.0, no_current_blocking))
        self.assertFalse(result_rows._is_better(0.8, 0.7, None, no_current_blocking))


if __name__ == "__main__":
    unittest.main()
