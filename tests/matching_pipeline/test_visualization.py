"""Tests for LightGlue keypoint-match visualization payloads."""

from __future__ import annotations

import unittest

from matching_pipeline.image_matching.visualization import build_keypoint_match_visualization


class KeypointVisualizationTests(unittest.TestCase):
    def test_builds_pixel_coordinate_matches_from_lightglue_pairs(self) -> None:
        payload = build_keypoint_match_visualization(
            {"keypoints": [[[10, 20], [30, 40]]]},
            {"keypoints": [[[50, 60], [70, 80]]]},
            {"matches": [[0, 1], [1, 0]], "scores": [0.9, 0.8]},
        )

        self.assertEqual(payload["coordinate_space"], "image_pixels")
        self.assertEqual(payload["match_count"], 2)
        self.assertEqual(
            payload["matches"][0],
            {
                "auction_keypoint_index": 0,
                "lost_keypoint_index": 1,
                "auction": {"x": 10.0, "y": 20.0},
                "lost": {"x": 70.0, "y": 80.0},
                "score": 0.9,
            },
        )

    def test_builds_from_matches0_fallback(self) -> None:
        payload = build_keypoint_match_visualization(
            {"keypoints": [[10, 20], [30, 40], [90, 100]]},
            {"keypoints": [[50, 60], [70, 80]]},
            {"matches0": [1, -1, 0], "matching_scores0": [0.9, 0.0, 0.7]},
        )

        self.assertEqual(payload["match_count"], 2)
        self.assertEqual(payload["matches"][0]["score"], 0.9)
        self.assertEqual(payload["matches"][1]["score"], 0.7)


if __name__ == "__main__":
    unittest.main()
