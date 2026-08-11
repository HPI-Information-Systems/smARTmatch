"""Validation and normalization tests for match visualizations."""

from __future__ import annotations

import unittest

from matching_pipeline.image_matching import visualization


class _TensorLike:
    def __init__(self, value):
        self.value = value
        self.detached = False
        self.on_cpu = False

    def detach(self):
        self.detached = True
        return self

    def cpu(self):
        self.on_cpu = True
        return self

    def tolist(self):
        return self.value


class VisualizationTests(unittest.TestCase):
    def test_pairs_scores_batches_negative_rows_and_short_scores(self) -> None:
        payload = visualization.build_keypoint_match_visualization(
            {"keypoints": [[[10, 20], [30, 40]]]},
            {"keypoints": [[[50, 60], [70, 80]]]},
            {
                "matches": [[[0, 1], [-1, 0], [1, -1], [1, 0]]],
                "scores": [[0.9]],
            },
        )
        self.assertEqual(payload["coordinate_space"], "image_pixels")
        self.assertEqual(payload["match_count"], 2)
        self.assertEqual(
            payload["matches"],
            [
                {
                    "auction_keypoint_index": 0,
                    "lost_keypoint_index": 1,
                    "auction": {"x": 10.0, "y": 20.0},
                    "lost": {"x": 70.0, "y": 80.0},
                    "score": 0.9,
                },
                {
                    "auction_keypoint_index": 1,
                    "lost_keypoint_index": 0,
                    "auction": {"x": 30.0, "y": 40.0},
                    "lost": {"x": 50.0, "y": 60.0},
                },
            ],
        )

    def test_matches0_matching_scores_and_tensor_normalization(self) -> None:
        auction = _TensorLike([[1, 2], [3, 4], [5, 6]])
        lost = _TensorLike([[7, 8], [9, 10]])
        matches0 = _TensorLike([[1, -1, 0]])
        scores0 = _TensorLike([[0.6, 0.0]])
        payload = visualization.build_keypoint_match_visualization(
            {"keypoints": auction},
            {"keypoints": lost},
            {"matches0": matches0, "matching_scores0": scores0},
        )
        self.assertEqual(payload["match_count"], 2)
        self.assertEqual(payload["matches"][0]["score"], 0.6)
        self.assertNotIn("score", payload["matches"][1])
        for value in (auction, lost, matches0, scores0):
            self.assertTrue(value.detached)
            self.assertTrue(value.on_cpu)

    def test_no_scores_produces_scoreless_payload(self) -> None:
        payload = visualization.build_keypoint_match_visualization(
            {"keypoints": [[1, 2]]},
            {"keypoints": [[3, 4]]},
            {"matches": [[0, 0]]},
        )
        self.assertNotIn("score", payload["matches"][0])

    def test_missing_or_invalid_pair_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing matches/matches0"):
            visualization.build_keypoint_match_visualization(
                {"keypoints": []}, {"keypoints": []}, {}
            )
        with self.assertRaisesRegex(ValueError, "two indices"):
            visualization.build_keypoint_match_visualization(
                {"keypoints": []},
                {"keypoints": []},
                {"matches": [[0]]},
            )
        with self.assertRaisesRegex(ValueError, "matches must be a row list"):
            visualization.build_keypoint_match_visualization(
                {"keypoints": []},
                {"keypoints": []},
                {"matches": "invalid"},
            )

    def test_keypoint_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "Missing auction keypoints"):
            visualization.build_keypoint_match_visualization(
                {}, {"keypoints": []}, {"matches": []}
            )
        with self.assertRaisesRegex(ValueError, "lost keypoints must be a row list"):
            visualization.build_keypoint_match_visualization(
                {"keypoints": []},
                {"keypoints": "bad"},
                {"matches": []},
            )
        with self.assertRaisesRegex(ValueError, "auction keypoint must have"):
            visualization.build_keypoint_match_visualization(
                {"keypoints": [[1]]},
                {"keypoints": []},
                {"matches": []},
            )

    def test_match_index_validation_for_both_images(self) -> None:
        with self.assertRaisesRegex(ValueError, "auction=1/1"):
            visualization.build_keypoint_match_visualization(
                {"keypoints": [[1, 2]]},
                {"keypoints": [[3, 4]]},
                {"matches": [[1, 0]]},
            )
        with self.assertRaisesRegex(ValueError, "lost=1/1"):
            visualization.build_keypoint_match_visualization(
                {"keypoints": [[1, 2]]},
                {"keypoints": [[3, 4]]},
                {"matches": [[0, 1]]},
            )

    def test_vector_validation_helpers(self) -> None:
        with self.assertRaisesRegex(ValueError, "Missing scores"):
            visualization._to_float_vector(None, "scores")
        with self.assertRaisesRegex(ValueError, "scores must be a vector"):
            visualization._to_float_vector("bad", "scores")
        self.assertEqual(visualization._to_int_vector([1.9], "values"), [1])
        self.assertEqual(visualization._to_float_vector([1], "values"), [1.0])

    def test_unwrap_helpers_leave_non_batches_unchanged(self) -> None:
        empty = []
        row = [[1, 2]]
        empty_batch = [[]]
        self.assertIs(visualization._unwrap_single_batch_rows(empty), empty)
        self.assertIs(visualization._unwrap_single_batch_rows(row), row)
        self.assertIs(
            visualization._unwrap_single_batch_rows(empty_batch), empty_batch
        )
        self.assertEqual(
            visualization._unwrap_single_batch_rows([[[1, 2]]]), [[1, 2]]
        )
        self.assertIs(visualization._unwrap_single_batch_vector(empty), empty)
        self.assertEqual(visualization._unwrap_single_batch_vector([[1]]), [1])
        self.assertEqual(visualization._to_python((1, 2)), (1, 2))


if __name__ == "__main__":
    unittest.main()
