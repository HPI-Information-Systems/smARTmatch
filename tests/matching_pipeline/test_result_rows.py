"""Tests for image match aggregation before DB writes."""

from __future__ import annotations

import unittest
from uuid import uuid4

from matching_pipeline.image_matching.result_rows import prepare_match_score_image_writes
from matching_pipeline.image_matching.results import AcceptedImageMatch


class MatchScoreImageWriteTests(unittest.TestCase):
    def test_picks_best_final_score_per_lost_auction_pair(self) -> None:
        lost_artwork_id = uuid4()
        auction_artwork_id = uuid4()
        program_id = uuid4()

        writes = prepare_match_score_image_writes(
            [
                AcceptedImageMatch("10", "auction-a.jpg", "20", "lost.jpg", 0.70, 0.90),
                AcceptedImageMatch("11", "auction-b.jpg", "20", "lost.jpg", 0.85, 0.40),
            ],
            auction_links={10: [auction_artwork_id], 11: [auction_artwork_id]},
            lost_links={20: [lost_artwork_id]},
            matching_program_id=program_id,
        )

        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].lost_artwork_id, lost_artwork_id)
        self.assertEqual(writes[0].auction_artwork_id, auction_artwork_id)
        self.assertEqual(writes[0].best_image_file_id, 10)
        self.assertEqual(writes[0].image_matching_confidence, 0.70)
        self.assertAlmostEqual(writes[0].image_final_score, 0.63)
        self.assertEqual(writes[0].image_blocking_similarity, 0.90)
        self.assertEqual(
            writes[0].image_visualization["image_matching"]["best_match"]["auction_image_file_id"],
            10,
        )

    def test_uses_confidence_as_final_score_tie_breaker(self) -> None:
        lost_artwork_id = uuid4()
        auction_artwork_id = uuid4()
        program_id = uuid4()

        writes = prepare_match_score_image_writes(
            [
                AcceptedImageMatch("10", "auction-a.jpg", "20", "lost.jpg", 0.60, 0.80),
                AcceptedImageMatch("11", "auction-b.jpg", "20", "lost.jpg", 0.80, 0.60),
            ],
            auction_links={10: [auction_artwork_id], 11: [auction_artwork_id]},
            lost_links={20: [lost_artwork_id]},
            matching_program_id=program_id,
        )

        self.assertEqual(writes[0].best_image_file_id, 11)
        self.assertAlmostEqual(writes[0].image_final_score, 0.48)
        self.assertEqual(writes[0].image_blocking_similarity, 0.60)

    def test_includes_keypoint_matches_in_visualization(self) -> None:
        lost_artwork_id = uuid4()
        auction_artwork_id = uuid4()
        program_id = uuid4()
        keypoint_matches = {
            "coordinate_space": "image_pixels",
            "match_count": 1,
            "matches": [
                {
                    "auction": {"x": 10.0, "y": 20.0},
                    "lost": {"x": 30.0, "y": 40.0},
                    "score": 0.9,
                }
            ],
        }

        writes = prepare_match_score_image_writes(
            [
                AcceptedImageMatch(
                    "10",
                    "auction-a.jpg",
                    "20",
                    "lost.jpg",
                    0.80,
                    0.50,
                    keypoint_matches,
                ),
            ],
            auction_links={10: [auction_artwork_id]},
            lost_links={20: [lost_artwork_id]},
            matching_program_id=program_id,
        )

        best_match = writes[0].image_visualization["image_matching"]["best_match"]
        self.assertEqual(best_match["keypoint_matches"], keypoint_matches)


if __name__ == "__main__":
    unittest.main()
