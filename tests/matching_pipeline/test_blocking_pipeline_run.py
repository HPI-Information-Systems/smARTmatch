"""Offline unit coverage for blocking pipeline orchestration."""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _blocking_test_support import patched_dirs

from matching_pipeline.image_blocking import pipeline
from matching_pipeline.image_blocking.db_updates import ExpectedImageVersion
from matching_pipeline.image_blocking.input_sources import ImageFileRow


def _source_identities(*file_ids: str) -> dict[str, dict[str, str]]:
    return {
        file_id: {"source_sha256": (str(index + 1) * 64)[:64]}
        for index, file_id in enumerate(file_ids)
    }


class PipelineRunTests(unittest.TestCase):
    def test_create_input_csv_uses_db_rows_and_effective_limit(self) -> None:
        rows = ([ImageFileRow("l", Path("l.jpg"))], [ImageFileRow("a", Path("a.jpg"))])
        output = Path("output.csv")
        with mock.patch.object(
            pipeline, "matching_batch_size_from_env", return_value=17
        ), mock.patch.object(
            pipeline, "load_db_image_file_rows", return_value=rows
        ) as load, mock.patch.object(
            pipeline, "blocking_input_csv_path", return_value=output
        ), mock.patch.object(
            pipeline, "write_image_file_csv", return_value=output
        ) as write:
            result = pipeline.create_blocking_input_csv(lost_limit=4)

        load.assert_called_once_with(
            lost_limit=4,
            auction_limit=17,
            include_processed_auction_images=False,
            validate_files=False,
        )
        write.assert_called_once_with(output, *rows)
        self.assertEqual(result, pipeline.BlockingInputCsvResult(output, 1, 1))

    def test_processed_replay_csv_resets_db_state_before_snapshot(self) -> None:
        rows = ([ImageFileRow("l", Path("l.jpg"))], [ImageFileRow("a", Path("a.jpg"))])
        output = Path("output.csv")
        with mock.patch.object(
            pipeline, "_prepare_processed_image_replay"
        ) as prepare, mock.patch.object(
            pipeline, "load_db_image_file_rows", return_value=rows
        ), mock.patch.object(
            pipeline, "blocking_input_csv_path", return_value=output
        ), mock.patch.object(
            pipeline, "write_image_file_csv", return_value=output
        ):
            pipeline.create_blocking_input_csv(
                include_processed_auction_images=True
            )

        prepare.assert_called_once_with()

    def test_run_skips_all_work_for_empty_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            candidates = root / "candidates"
            candidates.mkdir()
            stale = candidates / "part.parquet"
            stale.write_bytes(b"x")
            with patched_dirs(root, candidates), mock.patch.object(
                pipeline, "has_unprocessed_auction_image_file_rows", return_value=False
            ), mock.patch.object(pipeline, "_load_inputs") as load, mock.patch.object(
                pipeline, "write_image_files_parquet"
            ) as write_artifact:
                result = pipeline.run_image_blocking(clear_candidates=True)

            self.assertEqual(result, pipeline.BlockingRunResult(root, 0, 0, 0, 0, 0, 0))
            self.assertFalse(stale.exists())
            load.assert_not_called()
            write_artifact.assert_not_called()

    def test_processed_image_replay_resets_state_under_storage_lock_before_loading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patched_dirs(root, root / "candidates"), mock.patch.object(
                pipeline, "env_image_root", return_value=root
            ), mock.patch.object(
                pipeline,
                "reset_auction_image_matching_for_replay",
                return_value=(7, 3),
            ) as reset, mock.patch.object(
                pipeline, "_load_inputs", return_value=([], [])
            ) as load, mock.patch.object(
                pipeline, "write_image_files_parquet"
            ), mock.patch.object(
                pipeline, "clear_candidate_parts"
            ):
                result = pipeline.run_image_blocking(
                    include_processed_auction_images=True
                )

            reset.assert_called_once_with()
            load.assert_called_once()
            self.assertEqual(result.auction_image_count, 0)
            self.assertTrue((root / ".smartmatch-image-storage.lock").is_file())

    def test_run_with_no_auction_writes_snapshots_and_clears_parts(self) -> None:
        lost = [ImageFileRow("l", Path("lost.jpg"))]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            candidates = root / "candidates"
            with patched_dirs(root, candidates), mock.patch.object(
                pipeline, "_load_inputs", return_value=(lost, [])
            ), mock.patch.object(pipeline, "write_image_files_parquet") as write, mock.patch.object(
                pipeline, "clear_candidate_parts"
            ) as clear, mock.patch.object(
                pipeline, "ensure_lost_embedding_cache"
            ) as ensure:
                result = pipeline.run_image_blocking(input_csv=Path("input.csv"))

            self.assertEqual(result.lost_image_count, 1)
            self.assertEqual(result.auction_image_count, 0)
            self.assertEqual(write.call_count, 2)
            clear.assert_called_once_with(candidates)
            ensure.assert_not_called()

    def test_run_rejects_missing_lost_rows(self) -> None:
        auction = [ImageFileRow("a", Path("auction.jpg"))]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patched_dirs(root, root / "candidates"), mock.patch.object(
                pipeline, "_load_inputs", return_value=([], auction)
            ), mock.patch.object(pipeline, "write_image_files_parquet"):
                with self.assertRaisesRegex(ValueError, "No lost image files"):
                    pipeline.run_image_blocking(input_csv=Path("input.csv"))

    def test_full_csv_run_returns_counts_without_db_update(self) -> None:
        lost = [ImageFileRow("l1", Path("l1.jpg")), ImageFileRow("l2", Path("l2.jpg"))]
        auction = [ImageFileRow("a1", Path("a1.jpg"))]
        cache = SimpleNamespace(
            file_ids=["l1", "l2"],
            embeddings=SimpleNamespace(shape=(2, 8)),
            metadata={
                "generated_count": 1,
                "model_id": "test/dino-model",
                "source_identity_sha256": "lost-source-v1",
                "source_identities": _source_identities("l1", "l2"),
            },
        )
        fake_model = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patched_dirs(root, root / "candidates"), mock.patch.object(
                pipeline, "_load_inputs", return_value=(lost, auction)
            ), mock.patch.object(pipeline, "write_image_files_parquet"), mock.patch.object(
                pipeline, "ensure_lost_embedding_cache", return_value=cache
            ) as ensure, mock.patch.object(
                pipeline, "_write_candidates", return_value=(7, 3, 1)
            ) as candidates, mock.patch.object(
                pipeline,
                "source_identity_sha256",
                return_value="lost-source-v1",
            ), mock.patch.object(
                pipeline, "_ModelProvider", return_value=fake_model
            ), mock.patch.object(
                pipeline, "mark_image_files_embedded"
            ) as mark:
                result = pipeline.run_image_blocking(
                    input_csv=Path("input.csv"),
                    top_k=5,
                    image_batch_size=2,
                    candidate_shard_auction_images=4,
                    dtype="float32",
                    no_compile=True,
                    hf_token="token",
                )

            self.assertEqual(
                result,
                pipeline.BlockingRunResult(root, 2, 1, 7, 3, 1, 1, 0),
            )
            self.assertEqual(ensure.call_args.kwargs["model_factory"], fake_model.get)
            candidates.assert_called_once_with(
                auction,
                ["l1", "l2"],
                cache.embeddings,
                fake_model,
                5,
                2,
                4,
                model_identity="test/dino-model",
                lost_source_identity="lost-source-v1",
                lost_content_versions={"l1": None, "l2": None},
                lost_content_sha256={
                    file_id: identity["source_sha256"]
                    for file_id, identity in _source_identities("l1", "l2").items()
                },
            )
            mark.assert_not_called()

    def test_full_db_run_marks_all_embedded_ids(self) -> None:
        lost = [ImageFileRow("l", Path("l.jpg"), content_version=3)]
        auction = [ImageFileRow("a", Path("a.jpg"), content_version=4)]
        cache = SimpleNamespace(
            file_ids=["l"],
            embeddings=SimpleNamespace(shape=(1, 2)),
            metadata={
                "model_id": "test/dino-model",
                "source_identity_sha256": "lost-source-v1",
                "source_identities": _source_identities("l"),
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patched_dirs(root, root / "candidates"), mock.patch.object(
                pipeline, "has_unprocessed_auction_image_file_rows", return_value=True
            ), mock.patch.object(
                pipeline, "_load_inputs", return_value=(lost, auction)
            ), mock.patch.object(pipeline, "write_image_files_parquet"), mock.patch.object(
                pipeline, "ensure_lost_embedding_cache", return_value=cache
            ), mock.patch.object(
                pipeline, "_write_candidates", return_value=(1, 1, 0)
            ), mock.patch.object(
                pipeline,
                "source_identity_sha256",
                return_value="lost-source-v1",
            ), mock.patch.object(pipeline, "_ModelProvider"), mock.patch.object(
                pipeline, "mark_image_files_embedded", return_value=2
            ) as mark:
                result = pipeline.run_image_blocking(auction_limit=1)

            mark.assert_called_once_with(
                [ExpectedImageVersion("l", 3), ExpectedImageVersion("a", 4)]
            )
            self.assertEqual(result.generated_lost_embedding_count, 0)
            self.assertEqual(result.embedded_image_file_count, 2)

    def test_lost_source_change_after_candidates_clears_parts_and_fails(self) -> None:
        lost = [ImageFileRow("lost", Path("lost.jpg"))]
        auction = [ImageFileRow("auction", Path("auction.jpg"))]
        cache = SimpleNamespace(
            file_ids=["lost"],
            embeddings=SimpleNamespace(shape=(1, 2)),
            metadata={
                "model_id": "test/dino-model",
                "source_identity_sha256": "before",
                "source_identities": _source_identities("lost"),
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            candidates = root / "candidates"
            with patched_dirs(root, candidates), mock.patch.object(
                pipeline, "has_unprocessed_auction_image_file_rows", return_value=True
            ), mock.patch.object(
                pipeline, "_load_inputs", return_value=(lost, auction)
            ), mock.patch.object(
                pipeline, "write_image_files_parquet"
            ), mock.patch.object(
                pipeline, "ensure_lost_embedding_cache", return_value=cache
            ), mock.patch.object(
                pipeline, "_write_candidates", return_value=(1, 1, 0)
            ), mock.patch.object(
                pipeline, "source_identity_sha256", return_value="after"
            ), mock.patch.object(
                pipeline, "clear_candidate_parts"
            ) as clear, mock.patch.object(
                pipeline, "mark_image_files_embedded"
            ) as mark:
                with self.assertRaisesRegex(RuntimeError, "Lost images changed"):
                    pipeline.run_image_blocking(auction_limit=1)

            clear.assert_called_once_with(candidates)
            mark.assert_not_called()

    def test_pipeline_propagates_cache_and_candidate_failures(self) -> None:
        lost = [ImageFileRow("l", Path("l.jpg"))]
        auction = [ImageFileRow("a", Path("a.jpg"))]
        cache = SimpleNamespace(
            file_ids=["l"],
            embeddings=SimpleNamespace(shape=(1, 2)),
            metadata={
                "model_id": "test/dino-model",
                "source_identity_sha256": "lost-source-v1",
                "source_identities": _source_identities("l"),
            },
        )
        for target, side_effect in (
            ("ensure_lost_embedding_cache", RuntimeError("cache failed")),
            ("_write_candidates", RuntimeError("candidates failed")),
        ):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                patches = [
                    mock.patch.object(pipeline, "_load_inputs", return_value=(lost, auction)),
                    mock.patch.object(pipeline, "write_image_files_parquet"),
                    mock.patch.object(pipeline, "ensure_lost_embedding_cache", return_value=cache),
                    mock.patch.object(pipeline, "_write_candidates", return_value=(1, 1, 0)),
                    mock.patch.object(pipeline, target, side_effect=side_effect),
                ]
                with patched_dirs(root, root / "candidates"), contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with self.assertRaisesRegex(RuntimeError, str(side_effect)):
                        pipeline.run_image_blocking(input_csv=Path("input.csv"))


if __name__ == "__main__":
    unittest.main()
