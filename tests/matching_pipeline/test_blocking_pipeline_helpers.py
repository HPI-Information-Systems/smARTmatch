"""Offline unit coverage for blocking pipeline helper functions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _blocking_test_support import patched_dirs

from matching_pipeline.image_blocking import pipeline
from matching_pipeline.image_blocking.config import AUCTION_ROLE, LOST_ROLE
from matching_pipeline.image_blocking.db_updates import ExpectedImageVersion
from matching_pipeline.image_blocking.input_sources import ImageFileRow


class PipelineHelperTests(unittest.TestCase):
    def test_input_limit_and_skip_helpers(self) -> None:
        csv = Path("input.csv")
        rows = ([], [])
        with mock.patch.object(
            pipeline, "read_image_file_csv", return_value=rows
        ) as read:
            self.assertEqual(pipeline._load_inputs(csv, 1, 2, True), rows)
            read.assert_called_once_with(csv)
        with mock.patch.object(
            pipeline, "load_db_image_file_rows", return_value=rows
        ) as load:
            self.assertEqual(pipeline._load_inputs(None, 1, 2, True), rows)
            load.assert_called_once_with(
                lost_limit=1,
                auction_limit=2,
                include_processed_auction_images=True,
            )

        self.assertEqual(
            pipeline._effective_auction_limit(
                input_csv=csv, auction_limit=3, include_processed_auction_images=False
            ),
            3,
        )
        self.assertEqual(
            pipeline._effective_auction_limit(
                input_csv=None, auction_limit=4, include_processed_auction_images=True
            ),
            4,
        )
        with mock.patch.object(
            pipeline, "matching_batch_size_from_env", return_value=9
        ):
            self.assertEqual(
                pipeline._effective_auction_limit(
                    input_csv=None,
                    auction_limit=None,
                    include_processed_auction_images=False,
                ),
                9,
            )

        with mock.patch.object(
            pipeline, "has_unprocessed_auction_image_file_rows"
        ) as check:
            self.assertFalse(pipeline._should_skip_for_empty_db_auction_input(csv, False))
            self.assertFalse(pipeline._should_skip_for_empty_db_auction_input(None, True))
            check.assert_not_called()
        for present, expected in ((True, False), (False, True)):
            with mock.patch.object(
                pipeline,
                "has_unprocessed_auction_image_file_rows",
                return_value=present,
            ):
                self.assertIs(
                    pipeline._should_skip_for_empty_db_auction_input(None, False),
                    expected,
                )

    def test_filter_decodable_rows_skips_only_decode_failures(self) -> None:
        rows = [
            ImageFileRow("good", Path("good.jpg")),
            ImageFileRow("bad", Path("bad.jpg")),
        ]
        image = mock.Mock()
        with mock.patch.object(
            pipeline,
            "load_rgb_image",
            side_effect=[image, pipeline.ImageDecodeError("bad contents")],
        ), self.assertLogs(pipeline.logger, level="WARNING") as logs:
            self.assertEqual(pipeline._filter_decodable_rows("lost", rows), rows[:1])

        image.close.assert_called_once_with()
        messages = "\n".join(logs.output)
        self.assertIn("file_id=bad", messages)
        self.assertIn("Skipped 1/2 unreadable lost images", messages)

        with mock.patch.object(
            pipeline, "load_rgb_image", side_effect=RuntimeError("disk failed")
        ), self.assertLogs(pipeline.logger, level="ERROR") as errors:
            with self.assertRaisesRegex(RuntimeError, "disk failed"):
                pipeline._filter_decodable_rows("lost", rows)
        self.assertIn("file_id=good path=good.jpg", "\n".join(errors.output))

    def test_mark_write_and_logging_helpers(self) -> None:
        lost = [ImageFileRow("l", Path("l.jpg"), content_version=4)]
        auction = [ImageFileRow("a", Path("a.jpg"), content_version=5)]
        with mock.patch.object(pipeline, "mark_image_files_embedded", return_value=3) as mark:
            self.assertEqual(
                pipeline._mark_embedded_after_blocking(None, lost, auction), 3
            )
            mark.assert_called_once_with(
                [ExpectedImageVersion("l", 4), ExpectedImageVersion("a", 5)]
            )
        with mock.patch.object(pipeline, "mark_image_files_embedded") as mark:
            self.assertEqual(
                pipeline._mark_embedded_after_blocking(Path("x.csv"), lost, auction),
                0,
            )
            mark.assert_not_called()
        with self.assertRaisesRegex(ValueError, "missing content_version"):
            pipeline._mark_embedded_after_blocking(
                None,
                [ImageFileRow("legacy", Path("legacy.jpg"))],
                [],
            )

        with mock.patch.object(
            pipeline, "write_candidate_parts", return_value=(2, 1, 0)
        ) as write:
            model = mock.Mock()
            result = pipeline._write_candidates(
                auction,
                ["l"],
                "embeddings",
                model,
                2,
                3,
                4,
                model_identity="test/dino-model",
                lost_source_identity="lost-source-v1",
                lost_content_revision=None,
                lost_content_versions={"l": 7},
                lost_content_sha256={"l": "a" * 64},
            )
            self.assertEqual(result, (2, 1, 0))
            write.assert_called_once_with(
                auction,
                ["l"],
                "embeddings",
                model.get,
                model_identity="test/dino-model",
                lost_source_identity="lost-source-v1",
                lost_content_revision=None,
                lost_content_versions={"l": 7},
                lost_content_sha256={"l": "a" * 64},
                top_k=2,
                image_batch_size=3,
                shard_size=4,
            )

        self.assertEqual(
            pipeline._candidate_model_identity({"model_id": " test/dino-model "}),
            "test/dino-model",
        )
        with self.assertRaisesRegex(ValueError, "missing model_id"):
            pipeline._candidate_model_identity({})
        self.assertEqual(
            pipeline._candidate_lost_source_identity(
                {"source_identity_sha256": " source-v1 "}
            ),
            "source-v1",
        )
        with self.assertRaisesRegex(ValueError, "missing source_identity_sha256"):
            pipeline._candidate_lost_source_identity({})
        self.assertEqual(
            pipeline._candidate_lost_content_sha256(
                {"source_identities": {"l": {"source_sha256": "a" * 64}}},
                ["l"],
            ),
            {"l": "a" * 64},
        )
        with self.assertRaisesRegex(ValueError, "source_identities"):
            pipeline._candidate_lost_content_sha256({}, ["l"])

        result = pipeline.BlockingRunResult(
            Path("cache"), 3, 1, 8, 2, 0, 2, 4
        )
        with mock.patch.object(
            pipeline, "perf_counter", return_value=5.0
        ), self.assertLogs(pipeline.logger, level="INFO") as logs:
            self.assertIs(pipeline._log_blocking_finished(result, 1.0), result)
        summary = "\n".join(logs.output)
        self.assertIn("processable_images=4", summary)
        self.assertIn("throughput=1.00 images/s", summary)

    def test_model_provider_lazy_cache_and_dimension_validation(self) -> None:
        instance = mock.Mock()
        instance.get_dimension.return_value = 4
        instance.get_model_name.return_value = "fake-dino"
        adapter = mock.Mock(return_value=instance)
        with mock.patch.object(pipeline, "env_hf_token", return_value="resolved"), mock.patch.object(
            pipeline, "load_dino_adapter_class", return_value=adapter
        ):
            provider = pipeline._ModelProvider(no_compile=False, hf_token="cli")
            self.assertIs(provider.get(), instance)
            self.assertIs(provider.get(), instance)
        adapter.assert_called_once_with(use_compile=True, hf_token="resolved")

        invalid = mock.Mock()
        invalid.get_dimension.return_value = 0
        with mock.patch.object(pipeline, "env_hf_token", return_value=None), mock.patch.object(
            pipeline, "load_dino_adapter_class", return_value=mock.Mock(return_value=invalid)
        ):
            provider = pipeline._ModelProvider(no_compile=True, hf_token=None)
            with self.assertRaisesRegex(RuntimeError, "invalid dimension"):
                provider.get()

    def test_argument_validation_all_failures_and_success(self) -> None:
        pipeline._validate_args(1, 1, 1, None, None)
        for values, message in (
            ((0, 1, 1, None, None), "top_k must be positive"),
            ((1, 0, 1, None, None), "image_batch_size must be positive"),
            ((1, 1, 0, None, None), "candidate_shard_auction_images must be positive"),
            ((1, 1, 1, 0, None), "lost_limit must be positive"),
            ((1, 1, 1, None, -1), "auction_limit must be positive"),
            ((32_768, 1, 1, None, None), "top_k must fit"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    pipeline._validate_args(*values)

    def test_prepare_cache_directories_with_all_clear_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            candidates = root / "candidates"
            candidates.mkdir()
            (candidates / "stale").write_bytes(b"x")
            with patched_dirs(root, candidates):
                pipeline._prepare_cache_dirs(True)
                self.assertFalse((candidates / "stale").exists())
                pipeline._prepare_cache_dirs(False)
            self.assertTrue((root / LOST_ROLE).is_dir())
            self.assertTrue((root / AUCTION_ROLE).is_dir())
            self.assertTrue(candidates.is_dir())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            candidates = root / "absent"
            with patched_dirs(root, candidates):
                pipeline._prepare_cache_dirs(True)
            self.assertTrue(candidates.is_dir())


if __name__ == "__main__":
    unittest.main()
