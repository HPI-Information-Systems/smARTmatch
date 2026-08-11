"""Offline workflow tests for the lost-image embedding cache."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from matching_pipeline.image_blocking import embedding_cache as cache
from matching_pipeline.image_blocking.input_sources import ImageFileRow
from tests.matching_pipeline._blocking_cache_test_support import EmbeddingModel


class EmbeddingCacheWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model_id = "offline/test-model"
        self.model_patch = mock.patch.object(
            cache, "env_dinov3_model_id", return_value=self.model_id
        )
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)

    def test_build_cache_in_batches_normalizes_and_writes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "nested" / "lost.npz"
            rows = [ImageFileRow(f"l{i}", root / f"l{i}.jpg") for i in range(3)]
            vectors = {
                str(rows[0].file_path): [3, 0],
                str(rows[1].file_path): [0, 4],
                str(rows[2].file_path): [1, 1],
            }
            model = EmbeddingModel(vectors)

            result = cache.ensure_lost_embedding_cache(
                rows,
                cache_path=path,
                model_factory=lambda: model,
                batch_size=2,
                dtype="float16",
            )

            self.assertEqual(result.file_ids, ["l0", "l1", "l2"])
            np.testing.assert_allclose(np.linalg.norm(result.embeddings, axis=1), 1.0)
            self.assertEqual(result.embeddings.dtype, np.float32)
            self.assertEqual(result.metadata["generated_count"], 3)
            self.assertEqual(model.calls, [
                [str(rows[0].file_path), str(rows[1].file_path)],
                [str(rows[2].file_path)],
            ])
            self.assertTrue(path.is_file())
            self.assertEqual(list(path.parent.glob(f".{path.name}.tmp.*")), [])
            with np.load(path, allow_pickle=False) as data:
                self.assertEqual(data["embeddings"].dtype, np.float16)
                self.assertEqual(data["file_ids"].tolist(), ["l0", "l1", "l2"])

    def test_valid_cache_is_reused_without_model_or_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.npz"
            metadata = cache._metadata("float32", 2, 2)
            cache.write_lost_embedding_cache(
                path, ["a", "b"], np.asarray([[2, 0], [0, 3]], dtype=np.float32), metadata
            )
            rows = [ImageFileRow("a", Path(tmp) / "a.jpg"), ImageFileRow("b", Path(tmp) / "b.jpg")]

            with mock.patch.object(
                cache, "write_lost_embedding_cache"
            ) as write, mock.patch.object(
                cache, "_metadata", side_effect=AssertionError("metadata must be reused")
            ):
                result = cache.ensure_lost_embedding_cache(
                    rows,
                    cache_path=path,
                    model_factory=mock.Mock(side_effect=AssertionError("no inference")),
                    batch_size=8,
                    dtype="float32",
                )

            write.assert_not_called()
            self.assertEqual(result.metadata, metadata | {"generated_count": 0})
            np.testing.assert_array_equal(result.embeddings, np.eye(2, dtype=np.float32))

    def test_reorders_cached_rows_and_rewrites_requested_dtype(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.npz"
            cache.write_lost_embedding_cache(
                path,
                ["a", "b"],
                np.eye(2, dtype=np.float16),
                cache._metadata("float16", 2, 2),
            )
            rows = [ImageFileRow("b", Path(tmp) / "b.jpg"), ImageFileRow("a", Path(tmp) / "a.jpg")]
            result = cache.ensure_lost_embedding_cache(
                rows,
                cache_path=path,
                model_factory=mock.Mock(side_effect=AssertionError("no inference")),
                batch_size=1,
                dtype="float32",
            )
            self.assertEqual(result.file_ids, ["b", "a"])
            with np.load(path, allow_pickle=False) as data:
                self.assertEqual(data["file_ids"].tolist(), ["b", "a"])
                self.assertEqual(data["embeddings"].dtype, np.float32)

    def test_stale_model_and_dimension_metadata_rebuild_all_rows(self) -> None:
        for metadata in (
            {
                "model_id": "old-model",
                "embedding_dim": 2,
                "dtype": "float32",
            },
            {
                "model_id": self.model_id,
                "embedding_dim": 3,
                "dtype": "float32",
            },
        ):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "cache.npz"
                cache.write_lost_embedding_cache(path, ["a"], np.asarray([[1, 0]]), metadata)
                row = ImageFileRow("a", root / "a.jpg")
                model = EmbeddingModel({str(row.file_path): [0, 2]})
                result = cache.ensure_lost_embedding_cache(
                    [row], cache_path=path, model_factory=lambda: model, batch_size=1
                )
                self.assertEqual(result.metadata["generated_count"], 1)
                np.testing.assert_array_equal(result.embeddings, [[0.0, 1.0]])

    def test_missing_row_is_generated_and_merged_with_cached_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "cache.npz"
            cache.write_lost_embedding_cache(
                path, ["cached"], np.asarray([[5, 0]]), cache._metadata("float16", 1, 2)
            )
            rows = [ImageFileRow("cached", root / "c.jpg"), ImageFileRow("new", root / "n.jpg")]
            model = EmbeddingModel({str(rows[1].file_path): [0, 7]})
            result = cache.ensure_lost_embedding_cache(
                rows, cache_path=path, model_factory=lambda: model, batch_size=2
            )
            np.testing.assert_array_equal(result.embeddings, np.eye(2, dtype=np.float32))
            self.assertEqual(result.metadata["generated_count"], 1)


if __name__ == "__main__":
    unittest.main()
