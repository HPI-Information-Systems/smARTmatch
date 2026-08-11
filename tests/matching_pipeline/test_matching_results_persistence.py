"""Tests for structured image-matching persistence results."""

from __future__ import annotations

import unittest

from matching_pipeline.image_matching.results import AcceptedImageMatch, ImageMatchingRunResult


class StructuredPersistenceResultTests(unittest.TestCase):
    def test_accepted_match_csv_row_contains_only_csv_columns(self) -> None:
        match = AcceptedImageMatch(
            "10",
            "auction.jpg",
            "20",
            "lost.jpg",
            0.75,
            None,
            {"matches": [1]},
        )

        self.assertEqual(
            match.as_csv_row(),
            {
                "auction_file_id": "10",
                "auction_file_path": "auction.jpg",
                "lost_file_id": "20",
                "lost_file_path": "lost.jpg",
                "confidence": 0.75,
                "blocking_score": None,
            },
        )

    def test_run_result_length_counts_processed_files(self) -> None:
        result = ImageMatchingRunResult(["1", "2"], [], 5, 1, 2)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
