"""Offline unit tests for blocking candidate shard generation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from matching_pipeline.image_blocking import candidate_generation as candidates
from matching_pipeline.image_blocking.input_sources import ImageFileRow
from tests.matching_pipeline._blocking_cache_test_support import EmbeddingModel


class CandidateGenerationTests(unittest.TestCase):
    def test_clear_parts_explicit_and_environment_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            explicit = root / "explicit"
            explicit.mkdir()
            for name in ("part-000000-old.parquet", ".part-000001.tmp.7"):
                (explicit / name).write_bytes(b"stale")
            unrelated = explicit / "keep.txt"
            unrelated.write_bytes(b"keep")

            self.assertEqual(candidates.clear_candidate_parts(explicit), 2)
            self.assertEqual(list(explicit.iterdir()), [unrelated])

            configured = root / "configured"
            with mock.patch.object(
                candidates, "env_auction_to_lost_rankings_dir", return_value=configured
            ):
                self.assertEqual(candidates.clear_candidate_parts(), 0)
            self.assertTrue(configured.is_dir())

    def test_shards_resume_remove_stale_and_append_ranked_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            rows = [ImageFileRow(f"a{i}", output / f"a{i}.jpg") for i in range(3)]
            lost_ids = ["l0", "l1", "l2"]
            lost_embeddings = np.asarray([[1, 0], [0, 1], [-1, 0]], dtype=np.float32)
            lost_digest = candidates._digest_strings(lost_ids)
            resumed = candidates._part_path(output, 0, rows[:2], lost_digest, 2)
            resumed.write_bytes(b"complete")
            stale_zero = output / "part-000000-stale.parquet"
            stale_zero.write_bytes(b"stale")
            stale_one = output / "part-000001-stale.parquet"
            stale_one.write_bytes(b"stale")
            obsolete = output / "part-000002-obsolete.parquet"
            obsolete.write_bytes(b"obsolete")
            invalid = output / "part-bad.parquet"
            invalid.write_bytes(b"unparsed")
            temporary = output / ".part-000001.tmp.42"
            temporary.write_bytes(b"partial")

            model = EmbeddingModel({str(rows[2].file_path): [0, 2]})
            factory = mock.Mock(return_value=model)
            written: list[tuple[str, dict[str, list]]] = []

            def fake_write(name: str, **columns: list) -> None:
                written.append((name, columns))
                (output / name).write_bytes(b"parquet")

            with mock.patch.object(
                candidates, "env_auction_to_lost_rankings_dir", return_value=output
            ), mock.patch.object(
                candidates,
                "write_auction_to_lost_rankings_parquet",
                side_effect=fake_write,
            ):
                result = candidates.write_candidate_parts(
                    rows,
                    lost_ids,
                    lost_embeddings,
                    factory,
                    top_k=2,
                    image_batch_size=1,
                    shard_size=2,
                )

            self.assertEqual(result, (6, 2, 1))
            factory.assert_called_once_with()
            self.assertEqual(model.calls, [[str(rows[2].file_path)]])
            self.assertEqual(len(written), 1)
            self.assertEqual(
                written[0][1],
                {
                    "auction_file_ids": ["a2", "a2"],
                    "lost_file_ids": ["l1", "l0"],
                    "ranks": [1, 2],
                    "blocking_scores": [1.0, 0.0],
                },
            )
            self.assertTrue(resumed.exists())
            self.assertFalse(stale_zero.exists())
            self.assertFalse(stale_one.exists())
            self.assertFalse(obsolete.exists())
            self.assertFalse(temporary.exists())
            self.assertTrue(invalid.exists())

    def test_empty_plan_removes_old_parts_without_loading_a_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            old = output / "part-000000-old.parquet"
            old.write_bytes(b"old")
            factory = mock.Mock(side_effect=AssertionError("model must not load"))
            with mock.patch.object(
                candidates, "env_auction_to_lost_rankings_dir", return_value=output
            ):
                result = candidates.write_candidate_parts(
                    [], [], np.empty((0, 2)), factory, top_k=5, image_batch_size=2, shard_size=3
                )
            self.assertEqual(result, (0, 0, 0))
            self.assertFalse(old.exists())
            factory.assert_not_called()

    def test_candidate_helpers_cover_digests_indices_and_empty_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = ImageFileRow("auction", root / "auction.jpg")
            digest = candidates._digest_strings(["lost", 2])  # type: ignore[list-item]
            first = candidates._part_path(root, 4, [row], digest, 1)
            self.assertEqual(first, candidates._part_path(root, 4, [row], digest, 1))
            self.assertNotEqual(first, candidates._part_path(root, 4, [row], digest, 2))
            self.assertEqual(candidates._part_index(first), 4)
            self.assertIsNone(candidates._part_index(root / "part-bad.parquet"))
            self.assertIsNone(candidates._part_index(root / "other-1.parquet"))
            self.assertIsNone(candidates._part_index(root / "part"))
            self.assertEqual(candidates._part_count(5, 2), 3)
            self.assertEqual(candidates._empty_candidate_columns(), {
                "auction_file_ids": [],
                "lost_file_ids": [],
                "ranks": [],
                "blocking_scores": [],
            })


if __name__ == "__main__":
    unittest.main()
