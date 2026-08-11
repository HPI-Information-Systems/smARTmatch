"""Offline unit coverage for blocking pipeline helper functions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _blocking_test_support import patched_dirs
from matching_pipeline.image_blocking import pipeline
from matching_pipeline.image_blocking.config import AUCTION_ROLE, LOST_ROLE
from matching_pipeline.image_blocking.input_sources import ImageFileRow


class PipelineHelperTests(unittest.TestCase):
    def test_input_limit_and_skip_helpers(self) -> None:
        csv = Path("input.csv")
        with mock.patch.object(
            pipeline, "read_image_file_csv", return_value=mock.sentinel.csv_rows
        ) as read:
            self.assertIs(pipeline._load_inputs(csv, 1, 2, True), mock.sentinel.csv_rows)
            read.assert_called_once_with(csv)
        with mock.patch.object(
            pipeline, "load_db_image_file_rows", return_value=mock.sentinel.db_rows
        ) as load:
            self.assertIs(pipeline._load_inputs(None, 1, 2, True), mock.sentinel.db_rows)
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

    def test_mark_write_and_logging_helpers(self) -> None:
        auction = [ImageFileRow("a", Path("a.jpg"))]
        with mock.patch.object(pipeline, "mark_image_files_embedded", return_value=3) as mark:
            self.assertEqual(
                pipeline._mark_embedded_after_blocking(None, ["l"], auction), 3
            )
            mark.assert_called_once_with(["l", "a"])
        with mock.patch.object(pipeline, "mark_image_files_embedded") as mark:
            self.assertEqual(
                pipeline._mark_embedded_after_blocking(Path("x.csv"), ["l"], auction),
                0,
            )
            mark.assert_not_called()

        with mock.patch.object(
            pipeline, "write_candidate_parts", return_value=(2, 1, 0)
        ) as write:
            model = mock.Mock()
            result = pipeline._write_candidates(
                auction, ["l"], "embeddings", model, 2, 3, 4
            )
            self.assertEqual(result, (2, 1, 0))
            write.assert_called_once_with(
                auction,
                ["l"],
                "embeddings",
                model.get,
                top_k=2,
                image_batch_size=3,
                shard_size=4,
            )

        rows = [ImageFileRow(str(index), Path(str(index))) for index in range(4)]
        with self.assertLogs(pipeline.logger, level="DEBUG") as logs:
            pipeline._log_sample_rows("lost", rows)
            pipeline._log_sample_rows("lost", rows[:1])
        self.assertTrue(any("1 more rows" in message for message in logs.output))

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
