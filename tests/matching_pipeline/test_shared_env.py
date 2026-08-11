"""Offline unit tests for matching_pipeline.shared.env."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from matching_pipeline.shared import env
from tests.matching_pipeline._shared_test_support import TemporaryPipelineTest


class EnvironmentTests(TemporaryPipelineTest):
    def test_string_required_boolean_and_integer_helpers(self) -> None:
        os.environ.pop("VALUE", None)
        self.assertEqual(env.env_str("VALUE", "fallback"), "fallback")
        os.environ["VALUE"] = "   "
        self.assertIsNone(env.env_str("VALUE"))
        os.environ["VALUE"] = "  text  "
        self.assertEqual(env.env_str("VALUE"), "text")
        self.assertEqual(env.env_required_str("VALUE"), "text")

        os.environ.pop("VALUE", None)
        with self.assertRaisesRegex(ValueError, "VALUE is required"):
            env.env_required_str("VALUE")
        self.assertTrue(env.env_bool("VALUE", True))
        self.assertFalse(env.env_bool("VALUE"))
        for value in ("1", "TRUE", "Yes", "on"):
            os.environ["VALUE"] = value
            self.assertTrue(env.env_bool("VALUE"))
        os.environ["VALUE"] = "no"
        self.assertFalse(env.env_bool("VALUE", True))

        os.environ.pop("VALUE", None)
        self.assertIsNone(env.env_int("VALUE"))
        self.assertEqual(env.env_int("VALUE", 7), 7)
        os.environ["VALUE"] = " 12 "
        self.assertEqual(env.env_int("VALUE"), 12)
        os.environ["VALUE"] = "twelve"
        with self.assertRaisesRegex(ValueError, "VALUE must be an integer"):
            env.env_int("VALUE")

    def test_positive_integer_and_path_helpers(self) -> None:
        os.environ.pop("VALUE", None)
        self.assertEqual(env.env_positive_int("VALUE", 3), 3)
        for value in ("0", "-1"):
            os.environ["VALUE"] = value
            with self.assertRaisesRegex(ValueError, "positive integer"):
                env.env_positive_int("VALUE", 3)
        os.environ["VALUE"] = "2"
        self.assertEqual(env.env_positive_int("VALUE", 3), 2)

        os.environ.pop("VALUE", None)
        default = self.root / "default"
        self.assertEqual(env.env_path("VALUE", default), default)
        self.assertIsNone(env.env_path("VALUE"))
        os.environ["VALUE"] = str(self.root / "relative" / ".." / "resolved")
        self.assertEqual(env.env_path("VALUE"), (self.root / "resolved").resolve())

    def test_pipeline_defaults_paths_and_tokens(self) -> None:
        self.assertEqual(env.env_repo_root(), Path(env.__file__).resolve().parents[2])
        self.assertEqual(env.env_cache_dir(), self.cache_root.resolve())
        os.environ.pop("CACHE_DIR")
        self.assertEqual(env.env_cache_dir(), env.env_repo_root() / "cache")
        os.environ["CACHE_DIR"] = str(self.cache_root)
        self.assertEqual(env.env_image_root(), self.image_root.resolve())
        self.assertEqual(
            env.env_image_blocking_dir(), self.cache_root / "image_blocking"
        )
        self.assertEqual(
            env.env_image_files_parquet_path("auction"),
            self.cache_root / "image_blocking" / "auction" / "image_files.parquet",
        )
        self.assertEqual(
            env.env_image_files_parquet_path("lost"),
            self.cache_root / "image_blocking" / "lost" / "image_files.parquet",
        )
        with self.assertRaisesRegex(ValueError, "Invalid image-file artifact role"):
            env.env_image_files_parquet_path("other")
        self.assertEqual(
            env.env_auction_to_lost_rankings_dir(),
            self.cache_root / "image_blocking" / "auction_to_lost_candidates",
        )

        os.environ.pop("HF_TOKEN", None)
        self.assertIsNone(env.env_hf_token())
        os.environ["HF_TOKEN"] = "environment-token"
        self.assertEqual(env.env_hf_token(), "environment-token")
        self.assertEqual(env.env_hf_token("cli-token"), "cli-token")
        os.environ.pop("DINOV3_MODEL_ID", None)
        self.assertEqual(env.env_dinov3_model_id(), env.DEFAULT_DINOV3_MODEL_ID)
        os.environ["DINOV3_MODEL_ID"] = "local/model"
        self.assertEqual(env.env_dinov3_model_id(), "local/model")
        with mock.patch.object(env, "env_str", return_value=""):
            self.assertEqual(env.env_dinov3_model_id(), env.DEFAULT_DINOV3_MODEL_ID)
        self.assertFalse(env.env_non_gpu_inference_allowed())
        os.environ["ALLOW_NON_GPU_INFERENCE"] = "yes"
        self.assertTrue(env.env_non_gpu_inference_allowed())

    def test_image_root_is_required(self) -> None:
        os.environ.pop("SMARTMATCH_IMAGES_DIR")
        with self.assertRaisesRegex(ValueError, "SMARTMATCH_IMAGES_DIR is required"):
            env.env_image_root()
