"""Unit tests for image-blocking vector search."""

from __future__ import annotations

import unittest

import numpy as np

from matching_pipeline.image_blocking import search


class SearchTests(unittest.TestCase):
    def test_validation(self) -> None:
        valid = np.ones((1, 2), dtype=np.float32)
        with self.assertRaisesRegex(ValueError, "top_k must be positive"):
            search.topk_cosine_similarity(valid, valid, top_k=0)
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            search.topk_cosine_similarity(valid, np.empty((0, 2)), top_k=1)
        with self.assertRaisesRegex(ValueError, "does not match"):
            search.topk_cosine_similarity(valid, np.ones((1, 3)), top_k=1)

    def test_full_and_partial_top_k_search(self) -> None:
        queries = np.array([[1.0, 0.0], [0.0, 1.0]])
        candidates = np.array([[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]])
        indices, values = search.topk_cosine_similarity(queries, candidates, top_k=9)
        np.testing.assert_array_equal(indices, [[0, 1, 2], [1, 0, 2]])
        np.testing.assert_allclose(values, [[1.0, 0.0, -1.0], [1.0, 0.0, 0.0]])
        self.assertEqual(indices.dtype, np.int64)
        self.assertEqual(values.dtype, np.float32)

        indices, values = search.topk_cosine_similarity(queries[:1], candidates, top_k=2)
        np.testing.assert_array_equal(indices, [[0, 1]])
        np.testing.assert_allclose(values, [[1.0, 0.0]])


if __name__ == "__main__":
    unittest.main()
