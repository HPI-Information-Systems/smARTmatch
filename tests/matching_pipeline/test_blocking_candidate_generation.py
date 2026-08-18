"""Offline unit tests for blocking candidate shard generation."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from matching_pipeline.image_blocking import candidate_generation as candidates
from matching_pipeline.image_blocking.input_sources import ImageFileRow
from tests.matching_pipeline._blocking_cache_test_support import EmbeddingModel


def _lost_kwargs(file_ids: list[str]) -> dict[str, object]:
    return {
        "lost_content_versions": {file_id: 1 for file_id in file_ids},
        "lost_content_sha256": {
            file_id: hashlib.sha256(file_id.encode()).hexdigest()
            for file_id in file_ids
        },
    }


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
            for index, row in enumerate(rows):
                row.file_path.write_bytes(f"auction-{index}".encode())
            lost_ids = ["l0", "l1", "l2"]
            lost_embeddings = np.asarray([[1, 0], [0, 1], [-1, 0]], dtype=np.float32)
            lost_embedding_identity = candidates._embedding_identity(
                lost_ids,
                lost_embeddings,
                lost_source_identity="lost-source-v1",
                lost_content_versions=[1, 1, 1],
            )
            resumed = candidates._part_path(
                output,
                0,
                [candidates._image_identity(row) for row in rows[:2]],
                lost_embedding_identity,
                "test/dino-model",
                2,
            )
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
                    model_identity="test/dino-model",
                    lost_source_identity="lost-source-v1",
                    **_lost_kwargs(lost_ids),
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
                    "auction_content_versions": [None, None],
                    "auction_content_sha256": [
                        hashlib.sha256(b"auction-2").hexdigest(),
                        hashlib.sha256(b"auction-2").hexdigest(),
                    ],
                    "lost_file_ids": ["l1", "l0"],
                    "lost_content_versions": [1, 1],
                    "lost_content_sha256": [
                        hashlib.sha256(b"l1").hexdigest(),
                        hashlib.sha256(b"l0").hexdigest(),
                    ],
                    "lost_content_revisions": [None, None],
                    "ranks": [1, 2],
                    "blocking_scores": [1.0, 0.0],
                },
            )
            self.assertTrue(resumed.exists())
            self.assertFalse(stale_zero.exists())
            self.assertFalse(stale_one.exists())
            self.assertFalse(obsolete.exists())
            self.assertFalse(temporary.exists())
            self.assertFalse(invalid.exists())

    def test_database_invalidated_image_rebuilds_matching_candidate_part(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            image_path = output / "auction.jpg"
            image_path.write_bytes(b"auction")
            row = ImageFileRow("41", image_path, is_embedded=False)
            lost_ids = ["lost"]
            lost_embeddings = np.asarray([[1, 0]], dtype=np.float32)
            lost_source_identity = "lost-source-v1"
            existing = candidates._part_path(
                output,
                0,
                [candidates._image_identity(row)],
                candidates._embedding_identity(
                    lost_ids,
                    lost_embeddings,
                    lost_source_identity=lost_source_identity,
                    lost_content_versions=[1],
                ),
                "test/dino-model",
                1,
            )
            existing.write_bytes(b"stale")
            model = EmbeddingModel({str(image_path): [1, 0]})

            def fake_write(name: str, **_columns: list) -> None:
                (output / name).write_bytes(b"regenerated")

            with mock.patch.object(
                candidates, "env_auction_to_lost_rankings_dir", return_value=output
            ), mock.patch.object(
                candidates,
                "write_auction_to_lost_rankings_parquet",
                side_effect=fake_write,
            ):
                result = candidates.write_candidate_parts(
                    [row],
                    lost_ids,
                    lost_embeddings,
                    lambda: model,
                    model_identity="test/dino-model",
                    lost_source_identity=lost_source_identity,
                    **_lost_kwargs(lost_ids),
                    top_k=1,
                    image_batch_size=1,
                    shard_size=1,
                )

            self.assertEqual(result, (1, 1, 0))
            self.assertEqual(existing.read_bytes(), b"regenerated")
            self.assertEqual(model.calls, [[str(image_path)]])

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
                    [],
                    [],
                    np.empty((0, 2)),
                    factory,
                    model_identity="test/dino-model",
                    lost_source_identity="lost-source-v1",
                    **_lost_kwargs([]),
                    top_k=5,
                    image_batch_size=2,
                    shard_size=3,
                )
            self.assertEqual(result, (0, 0, 0))
            self.assertFalse(old.exists())
            factory.assert_not_called()

    def test_identity_failure_clears_stale_parts_before_failing(self) -> None:
        cases = (
            (
                [ImageFileRow("auction", Path("missing.jpg"))],
                ["lost"],
                np.asarray([[1.0, 0.0]], dtype=np.float32),
                (FileNotFoundError, "missing.jpg"),
            ),
            (
                [],
                ["lost"],
                np.asarray([[np.nan, 0.0]], dtype=np.float32),
                (ValueError, "NaN or infinite"),
            ),
        )
        for rows, lost_ids, embeddings, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp)
                stale = output / "part-000000-stale.parquet"
                stale.write_bytes(b"stale")
                factory = mock.Mock(side_effect=AssertionError("model must not load"))
                with mock.patch.object(
                    candidates,
                    "env_auction_to_lost_rankings_dir",
                    return_value=output,
                ):
                    with self.assertRaisesRegex(*expected):
                        candidates.write_candidate_parts(
                            rows,
                            lost_ids,
                            embeddings,
                            factory,
                            model_identity="test/dino-model",
                            lost_source_identity="lost-source-v1",
                            **_lost_kwargs(lost_ids),
                            top_k=1,
                            image_batch_size=1,
                            shard_size=1,
                        )
                self.assertFalse(stale.exists())
                factory.assert_not_called()

    def test_database_digest_mismatch_clears_candidate_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            image_path = output / "auction.jpg"
            image_path.write_bytes(b"current")
            stale = output / "part-000000-stale.parquet"
            stale.write_bytes(b"stale")
            row = ImageFileRow(
                "auction",
                image_path,
                content_version=8,
                content_sha256=hashlib.sha256(b"previous").hexdigest(),
            )

            with mock.patch.object(
                candidates,
                "env_auction_to_lost_rankings_dir",
                return_value=output,
            ):
                with self.assertRaisesRegex(RuntimeError, "database digest"):
                    candidates.write_candidate_parts(
                        [row],
                        ["lost"],
                        np.asarray([[1.0, 0.0]], dtype=np.float32),
                        mock.Mock(),
                        model_identity="test/dino-model",
                        lost_source_identity="lost-source-v1",
                        **_lost_kwargs(["lost"]),
                        top_k=1,
                        image_batch_size=1,
                        shard_size=1,
                    )

            self.assertFalse(stale.exists())

    def test_embedding_failure_logs_specific_auction_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            image_path = output / "auction.jpg"
            image_path.write_bytes(b"auction")
            row = ImageFileRow("auction-id", image_path)
            model = mock.Mock()
            model.generate_embeddings_batch.side_effect = RuntimeError("inference failed")

            with mock.patch.object(
                candidates, "env_auction_to_lost_rankings_dir", return_value=output
            ), self.assertLogs(candidates.logger, level="ERROR") as errors:
                with self.assertRaisesRegex(RuntimeError, "inference failed"):
                    candidates.write_candidate_parts(
                        [row],
                        ["lost"],
                        np.asarray([[1.0, 0.0]], dtype=np.float32),
                        lambda: model,
                        model_identity="test/dino-model",
                        lost_source_identity="lost-source-v1",
                        **_lost_kwargs(["lost"]),
                        top_k=1,
                        image_batch_size=1,
                        shard_size=1,
                    )

            output_text = "\n".join(errors.output)
            self.assertIn("file_id=auction-id", output_text)
            self.assertIn(str(image_path), output_text)

    def test_source_change_during_generation_clears_candidate_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            image_path = output / "auction.jpg"
            image_path.write_bytes(b"before")
            rows = [ImageFileRow("auction", image_path)]

            class MutatingModel:
                def generate_embeddings_batch(self, _paths):
                    image_path.write_bytes(b"after")
                    return np.asarray([[1.0, 0.0]], dtype=np.float32)

            with mock.patch.object(
                candidates,
                "env_auction_to_lost_rankings_dir",
                return_value=output,
            ), mock.patch.object(
                candidates, "write_auction_to_lost_rankings_parquet"
            ) as write:
                with self.assertRaisesRegex(RuntimeError, "changed during"):
                    candidates.write_candidate_parts(
                        rows,
                        ["lost"],
                        np.asarray([[1.0, 0.0]], dtype=np.float32),
                        MutatingModel,
                        model_identity="test/dino-model",
                        lost_source_identity="lost-source-v1",
                        **_lost_kwargs(["lost"]),
                        top_k=1,
                        image_batch_size=1,
                        shard_size=1,
                    )

            self.assertEqual(list(output.glob("part-*.parquet")), [])
            write.assert_not_called()

    def test_lost_content_revision_changes_parts_and_candidate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "auction.jpg"
            image_path.write_bytes(b"auction")
            row = ImageFileRow("auction", image_path)
            image_identity = candidates._image_identity(row)
            base_args = (
                root,
                0,
                [image_identity],
                "lost-embedding",
                "model",
                1,
            )
            self.assertNotEqual(
                candidates._part_path(*base_args, lost_content_revision=1),
                candidates._part_path(*base_args, lost_content_revision=2),
            )

            columns = candidates._empty_candidate_columns()
            candidates._append_candidates(
                columns,
                [row],
                ["lost"],
                np.asarray([[0]]),
                np.asarray([[0.5]]),
                [image_identity],
                [3],
                ["a" * 64],
                7,
            )
            self.assertEqual(columns["lost_content_revisions"], [7])

    def test_candidate_helpers_cover_identity_indices_and_empty_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_path = root / "auction.jpg"
            image_path.write_bytes(b"auction-v1")
            row = ImageFileRow("auction", image_path)
            embeddings = np.asarray([[1.0, 0.0]], dtype=np.float32)
            embedding_identity = candidates._embedding_identity(
                ["lost"],
                embeddings,
                lost_source_identity="lost-source-v1",
            )
            image_identity = candidates._image_identity(row)
            first = candidates._part_path(
                root,
                4,
                [image_identity],
                embedding_identity,
                "model-v1",
                1,
            )
            self.assertEqual(
                first,
                candidates._part_path(
                    root,
                    4,
                    [image_identity],
                    embedding_identity,
                    "model-v1",
                    1,
                ),
            )
            self.assertNotEqual(
                first,
                candidates._part_path(
                    root,
                    4,
                    [image_identity],
                    embedding_identity,
                    "model-v1",
                    2,
                ),
            )
            self.assertNotEqual(
                first,
                candidates._part_path(
                    root,
                    4,
                    [image_identity],
                    embedding_identity,
                    "model-v2",
                    1,
                ),
            )
            changed_version = candidates._embedding_identity(
                ["lost"],
                embeddings,
                lost_source_identity="lost-source-v1",
                lost_content_versions=[2],
            )
            self.assertNotEqual(embedding_identity, changed_version)
            changed_embeddings = candidates._embedding_identity(
                ["lost"],
                np.asarray([[0.0, 1.0]], dtype=np.float32),
                lost_source_identity="lost-source-v1",
            )
            self.assertNotEqual(
                first,
                candidates._part_path(
                    root,
                    4,
                    [image_identity],
                    changed_embeddings,
                    "model-v1",
                    1,
                ),
            )
            changed_lost_source = candidates._embedding_identity(
                ["lost"],
                embeddings,
                lost_source_identity="lost-source-v2",
            )
            self.assertNotEqual(
                first,
                candidates._part_path(
                    root,
                    4,
                    [image_identity],
                    changed_lost_source,
                    "model-v1",
                    1,
                ),
            )
            image_path.write_bytes(b"auction-v2")
            changed_image_identity = candidates._image_identity(row)
            self.assertNotEqual(
                first,
                candidates._part_path(
                    root,
                    4,
                    [changed_image_identity],
                    embedding_identity,
                    "model-v1",
                    1,
                ),
            )
            alternate_path = root / "moved.jpg"
            alternate_path.write_bytes(b"auction-v1")
            moved_identity = candidates._image_identity(
                ImageFileRow("auction", alternate_path)
            )
            self.assertNotEqual(
                first,
                candidates._part_path(
                    root,
                    4,
                    [moved_identity],
                    embedding_identity,
                    "model-v1",
                    1,
                ),
            )
            self.assertEqual(candidates._part_index(first), 4)
            self.assertIsNone(candidates._part_index(root / "part-bad.parquet"))
            self.assertIsNone(candidates._part_index(root / "other-1.parquet"))
            self.assertIsNone(candidates._part_index(root / "part"))
            self.assertEqual(candidates._part_count(5, 2), 3)
            self.assertEqual(
                candidates._empty_candidate_columns(),
                {
                    "auction_file_ids": [],
                    "auction_content_versions": [],
                    "auction_content_sha256": [],
                    "lost_file_ids": [],
                    "lost_content_versions": [],
                    "lost_content_sha256": [],
                    "lost_content_revisions": [],
                    "ranks": [],
                    "blocking_scores": [],
                },
            )


if __name__ == "__main__":
    unittest.main()
