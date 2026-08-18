"""Validation and failure tests for the lost-image embedding cache."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from matching_pipeline.image_blocking import embedding_cache as cache
from matching_pipeline.image_blocking.input_sources import ImageFileRow
from tests.matching_pipeline._blocking_cache_test_support import EmbeddingModel


class EmbeddingCacheValidationTests(unittest.TestCase):
    def test_load_missing_cache_and_dynamic_adapter_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(cache.load_lost_embedding_cache(Path(tmp) / "missing.npz"))
        adapter = object()
        module = types.SimpleNamespace(DinoV3Adapter=adapter)
        with mock.patch.dict(sys.modules, {"matching_pipeline.image_blocking.dino_adapter": module}):
            self.assertIs(cache.load_dino_adapter_class(), adapter)

    def test_load_wraps_archive_and_metadata_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed = root / "malformed.npz"
            malformed.write_bytes(b"not an npz")
            with self.assertRaisesRegex(ValueError, "Invalid lost embedding cache"):
                cache.load_lost_embedding_cache(malformed)

            bad_json = root / "bad-json.npz"
            np.savez(
                bad_json,
                file_ids=np.asarray(["a"]),
                embeddings=np.asarray([[1.0]]),
                metadata=np.asarray("{"),
            )
            with self.assertRaisesRegex(ValueError, "Invalid lost embedding cache"):
                cache.load_lost_embedding_cache(bad_json)

    def test_load_rejects_invalid_embedding_arrays(self) -> None:
        arrays = (
            np.asarray([1.0, 2.0]),
            np.empty((1, 0)),
            np.asarray([[np.nan, 1.0]]),
            np.asarray([[np.inf, 1.0]]),
        )
        for index, embeddings in enumerate(arrays):
            with self.subTest(embeddings=embeddings.shape), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"bad-{index}.npz"
                np.savez(
                    path,
                    file_ids=np.asarray(["a"]),
                    embeddings=embeddings,
                    metadata=np.asarray(json.dumps({"embedding_dim": 2})),
                )
                with self.assertRaises(ValueError):
                    cache.load_lost_embedding_cache(path)

    def test_normalize_rejects_dimension_nonfinite_and_zero_vectors(self) -> None:
        with self.assertRaisesRegex(ValueError, "Expected embedding dimension 3"):
            cache.normalize_embeddings(np.ones((1, 2)), expected_dim=3)
        with self.assertRaisesRegex(ValueError, "zero vectors"):
            cache.normalize_embeddings(np.asarray([[0.0, 0.0]]))

    def test_generate_and_configuration_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "batch_size must be positive"):
                cache.ensure_lost_embedding_cache(
                    [], cache_path=Path(tmp) / "x.npz", model_factory=mock.Mock(), batch_size=0
                )

            row = ImageFileRow("a", Path(tmp) / "a.jpg")
            bad_dimension = EmbeddingModel({str(row.file_path): [1, 0]}, dimension=0)
            with self.assertRaisesRegex(RuntimeError, "invalid dimension"):
                cache._generate_embeddings(bad_dimension, [row], 1)

            wrong_width = EmbeddingModel({str(row.file_path): [1, 0, 0]}, dimension=2)
            with self.assertLogs(cache.logger, level="ERROR") as errors:
                with self.assertRaisesRegex(ValueError, "Expected embedding dimension 2"):
                    cache._generate_embeddings(wrong_width, [row], 1)
            self.assertIn("file_id=a", "\n".join(errors.output))
            self.assertIn(str(row.file_path), "\n".join(errors.output))

        with self.assertRaisesRegex(ValueError, "dtype must be"):
            cache._numpy_dtype("float64")
        self.assertEqual(cache._numpy_dtype("float32"), np.dtype("float32"))
        self.assertEqual(cache._batch_count(5, 2), 3)

    def test_cached_validation_rejects_zero_vectors_and_mismatched_lengths(self) -> None:
        sources = {"a": {"source_sha256": "a"}}
        metadata = {
            "model_id": "model",
            "embedding_dim": 2,
            "dtype": "float32",
            "source_identities": sources,
            "source_identity_sha256": cache._source_identity_sha256(sources),
        }
        with mock.patch.object(cache, "env_dinov3_model_id", return_value="model"):
            with self.assertRaisesRegex(ValueError, "zero vectors"):
                cache._valid_cached_embeddings(
                    cache.LostEmbeddingCache(["a"], np.asarray([[0.0, 0.0]]), metadata),
                    sources,
                )
            with self.assertRaises(ValueError):
                cache._valid_cached_embeddings(
                    cache.LostEmbeddingCache(
                        ["a", "b"], np.asarray([[1.0, 0.0]]), metadata
                    ),
                    sources,
                )

    def test_metadata_dimension_and_cache_rewrite_conditions(self) -> None:
        self.assertEqual(cache._metadata_embedding_dim({"embedding_dim": "4"}), 4)
        self.assertEqual(cache._metadata_embedding_dim({"embedding_dim": object()}), -1)
        self.assertEqual(cache._metadata_embedding_dim({"embedding_dim": "bad"}), -1)
        self.assertEqual(cache._string_array([]).dtype, np.dtype("<U1"))

        sources = {"a": {"source_sha256": "a"}}
        item = cache.LostEmbeddingCache(
            ["a"], np.asarray([[1.0, 0.0]]), {
                "model_id": "model",
                "embedding_dim": 2,
                "dtype": "float32",
                "source_identities": sources,
                "source_identity_sha256": cache._source_identity_sha256(sources),
            }
        )
        with mock.patch.object(cache, "env_dinov3_model_id", return_value="model"):
            self.assertTrue(
                cache._cache_needs_rewrite(None, ["a"], "float32", 2, sources)
            )
            self.assertTrue(
                cache._cache_needs_rewrite(item, ["b"], "float32", 2, sources)
            )
            self.assertTrue(
                cache._cache_needs_rewrite(item, ["a"], "float16", 2, sources)
            )
            self.assertTrue(
                cache._cache_needs_rewrite(item, ["a"], "float32", 3, sources)
            )
            self.assertFalse(
                cache._cache_needs_rewrite(item, ["a"], "float32", 2, sources)
            )
        with mock.patch.object(cache, "env_dinov3_model_id", return_value="new-model"):
            self.assertTrue(
                cache._cache_needs_rewrite(item, ["a"], "float32", 2, sources)
            )

    def test_atomic_write_reports_save_and_replace_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "cache.npz"
            with mock.patch.object(cache.np, "savez", side_effect=OSError("save failed")):
                with self.assertRaisesRegex(OSError, "save failed"):
                    cache.write_lost_embedding_cache(
                        target, ["a"], np.ones((1, 1)), {"model_id": "model"}
                    )
            self.assertFalse(target.exists())

            with mock.patch.object(cache.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    cache.write_lost_embedding_cache(
                        target, ["a"], np.ones((1, 1)), {"model_id": "model"}
                    )
            self.assertFalse(target.exists())
            self.assertEqual(len(list(root.glob(f".{target.name}.tmp.*.npz"))), 0)


if __name__ == "__main__":
    unittest.main()
