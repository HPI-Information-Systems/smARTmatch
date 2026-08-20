"""Unit tests for DINO adapter pooling and construction."""

from __future__ import annotations

import types
import unittest
from unittest import mock

import torch

from matching_pipeline.image_blocking import dino_adapter
from tests.matching_pipeline._blocking_dino_db_test_support import FakeModel


class PoolingHelperTests(unittest.TestCase):
    def test_pad_color_clamps_and_scales_values(self) -> None:
        self.assertEqual(
            dino_adapter.pad_color_from_mean((-2.0, 0.5, 300.0)),
            (0, 128, 255),
        )

    def test_pooling_mode_validation_and_aliases(self) -> None:
        self.assertEqual(dino_adapter.normalize_pooling_mode(" Mean "), "avg")
        self.assertEqual(dino_adapter.normalize_pooling_mode("MAX"), "max")
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            dino_adapter.normalize_pooling_mode("  ")

        supported = frozenset({"avg", "max"})
        self.assertEqual(
            dino_adapter.normalize_pooling_modes(["mean", "max", "avg"], supported),
            ("avg", "max"),
        )
        self.assertEqual(
            dino_adapter.normalize_pooling_modes("average", supported), ("avg",)
        )
        with self.assertRaisesRegex(ValueError, "At least one"):
            dino_adapter.normalize_pooling_modes([], supported)
        with self.assertRaisesRegex(ValueError, "Unsupported pooling.*median"):
            dino_adapter.normalize_pooling_modes(["median"], supported)

    def test_unmasked_token_aggregation_and_validation(self) -> None:
        tokens = torch.tensor([[[1.0, 8.0], [3.0, 2.0], [9.0, 4.0]]])
        expected = {
            "avg": [[13.0 / 3.0, 14.0 / 3.0]],
            "median": [[3.0, 4.0]],
            "min": [[1.0, 2.0]],
            "max": [[9.0, 8.0]],
        }
        for mode, values in expected.items():
            torch.testing.assert_close(
                dino_adapter.aggregate_tokens(tokens, mode), torch.tensor(values)
            )
        with self.assertRaisesRegex(ValueError, "shape"):
            dino_adapter.aggregate_tokens(torch.ones(2, 3), "avg")
        with self.assertRaisesRegex(ValueError, "empty token"):
            dino_adapter.aggregate_tokens(torch.empty(1, 0, 2), "avg")
        with self.assertRaisesRegex(ValueError, "Unsupported token"):
            dino_adapter.aggregate_tokens(tokens, "cls")
        with self.assertRaisesRegex(AssertionError, "Unhandled"):
            dino_adapter._aggregate_unmasked_tokens(tokens, "other")

    def test_masked_token_aggregation_and_validation(self) -> None:
        tokens = torch.tensor(
            [
                [[1.0, 9.0], [3.0, 7.0], [100.0, 100.0]],
                [[8.0, 2.0], [4.0, 6.0], [2.0, 10.0]],
            ]
        )
        mask = torch.tensor([[True, True, False], [False, True, True]])
        expected = {
            "avg": [[2.0, 8.0], [3.0, 8.0]],
            "median": [[1.0, 7.0], [2.0, 6.0]],
            "min": [[1.0, 7.0], [2.0, 6.0]],
            "max": [[3.0, 9.0], [4.0, 10.0]],
        }
        for mode, values in expected.items():
            torch.testing.assert_close(
                dino_adapter.aggregate_tokens(tokens, mode, mask),
                torch.tensor(values),
            )
        with self.assertRaisesRegex(ValueError, "token_mask with shape"):
            dino_adapter.aggregate_tokens(tokens, "avg", torch.ones(2, 2))
        with self.assertRaisesRegex(ValueError, "no valid tokens"):
            dino_adapter.aggregate_tokens(tokens, "avg", torch.zeros(2, 3))
        with self.assertRaisesRegex(AssertionError, "Unhandled"):
            dino_adapter._aggregate_masked_tokens(tokens, "other", mask)


class DinoAdapterConstructionTests(unittest.TestCase):
    def test_cuda_preflight_uses_cached_parameter_count_before_model_load(self) -> None:
        metadata = dino_adapter.HfSafetensorsMetadata(
            model_id="org/dino",
            requested_revision="main",
            resolved_revision="resolved-sha",
            parameter_counts_by_dtype={"F32": 100},
            total_parameters=100,
        )
        snapshot = mock.Mock()
        with (
            mock.patch.object(
                dino_adapter,
                "get_cached_hf_safetensors_metadata",
                return_value=metadata,
            ) as metadata_cache,
            mock.patch.object(
                dino_adapter,
                "log_cuda_memory",
                return_value=snapshot,
            ) as memory_log,
            mock.patch.object(dino_adapter, "require_cuda_memory") as require,
        ):
            revision = dino_adapter._preflight_dino_model_metadata(
                "org/dino",
                token="secret",
                runtime_dtype=torch.float16,
            )

        self.assertEqual(revision, "resolved-sha")
        metadata_cache.assert_called_once_with("org/dino", token="secret")
        memory_log.assert_called_once_with(
            dino_adapter.logger,
            context="before DINOv3 checkpoint load",
            device="cuda",
        )
        require.assert_called_once_with(
            snapshot,
            200,
            component="DINOv3",
            basis="allocator_available",
        )

    def test_cuda_metadata_failure_is_fail_open(self) -> None:
        with mock.patch.object(
            dino_adapter,
            "get_cached_hf_safetensors_metadata",
            side_effect=OSError("offline"),
        ), mock.patch.object(
            dino_adapter, "log_cuda_memory"
        ) as memory_log, self.assertLogs(
            dino_adapter.logger, level="WARNING"
        ):
            revision = dino_adapter._preflight_dino_model_metadata(
                "org/dino",
                token=None,
                runtime_dtype=torch.bfloat16,
            )

        self.assertIsNone(revision)
        memory_log.assert_not_called()

    def test_insufficient_memory_remains_fatal_if_failure_logging_breaks(self) -> None:
        metadata = dino_adapter.HfSafetensorsMetadata(
            model_id="org/dino",
            requested_revision="main",
            resolved_revision="resolved-sha",
            parameter_counts_by_dtype={"F32": 100},
            total_parameters=100,
        )
        snapshot = mock.Mock()
        with (
            mock.patch.object(
                dino_adapter,
                "get_cached_hf_safetensors_metadata",
                return_value=metadata,
            ),
            mock.patch.object(
                dino_adapter,
                "log_cuda_memory",
                side_effect=[snapshot, RuntimeError("logging failed")],
            ),
            mock.patch.object(
                dino_adapter,
                "require_cuda_memory",
                side_effect=dino_adapter.InsufficientGpuMemoryError("too small"),
            ),
            self.assertLogs(dino_adapter.logger, level="ERROR"),
            self.assertRaises(dino_adapter.InsufficientGpuMemoryError),
        ):
            dino_adapter._preflight_dino_model_metadata(
                "org/dino",
                token=None,
                runtime_dtype=torch.float16,
            )

    def test_model_resolution_uses_only_explicit_or_environment_id(self) -> None:
        adapter = dino_adapter.DinoV3Adapter
        self.assertEqual(adapter._resolve_model_id(" custom/model "), "custom/model")
        with mock.patch.object(
            dino_adapter, "env_dinov3_model_id", return_value="env/model"
        ) as environment_model:
            self.assertEqual(adapter._resolve_model_id(None), "env/model")
            self.assertEqual(adapter._resolve_model_id("   "), "env/model")
        self.assertEqual(environment_model.call_count, 2)

    def test_device_selection(self) -> None:
        with mock.patch.object(torch.cuda, "is_available", return_value=True):
            self.assertEqual(dino_adapter.DinoV3Adapter._select_device(), "cuda")
        with mock.patch.object(
            torch.cuda, "is_available", return_value=False
        ), mock.patch.object(
            dino_adapter, "env_non_gpu_inference_allowed", return_value=False
        ):
            with self.assertRaisesRegex(RuntimeError, "CUDA GPU not detected"):
                dino_adapter.DinoV3Adapter._select_device()
        with mock.patch.object(
            torch.cuda, "is_available", return_value=False
        ), mock.patch.object(
            dino_adapter, "env_non_gpu_inference_allowed", return_value=True
        ), mock.patch.object(torch.backends.mps, "is_available", return_value=True):
            self.assertEqual(dino_adapter.DinoV3Adapter._select_device(), "mps")
        with mock.patch.object(
            torch.cuda, "is_available", return_value=False
        ), mock.patch.object(
            dino_adapter, "env_non_gpu_inference_allowed", return_value=True
        ), mock.patch.object(torch.backends.mps, "is_available", return_value=False):
            self.assertEqual(dino_adapter.DinoV3Adapter._select_device(), "cpu")
        fake_torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
            backends=types.SimpleNamespace(),
        )
        with mock.patch.object(dino_adapter, "torch", fake_torch), mock.patch.object(
            dino_adapter, "env_non_gpu_inference_allowed", return_value=True
        ):
            self.assertEqual(dino_adapter.DinoV3Adapter._select_device(), "cpu")

    def test_init_cpu_processor_without_token(self) -> None:
        model = FakeModel()
        processor = mock.Mock(name="processor")
        with mock.patch.object(
            dino_adapter.DinoV3Adapter, "_select_device", return_value="cpu"
        ), mock.patch.object(
            dino_adapter, "env_hf_token", return_value=None
        ), mock.patch.object(
            dino_adapter.AutoModel, "from_pretrained", return_value=model
        ) as load_model, mock.patch.object(
            dino_adapter.AutoImageProcessor,
            "from_pretrained",
            return_value=processor,
        ) as load_processor:
            adapter = dino_adapter.DinoV3Adapter(
                model_id="test/dino-small",
                use_compile=True,
            )

        self.assertIs(adapter.model, model)
        self.assertIs(adapter.processor, processor)
        self.assertEqual(model.to_device, "cpu")
        self.assertTrue(model.eval_called)
        load_model.assert_called_once_with(
            "test/dino-small",
            trust_remote_code=True,
        )
        load_processor.assert_called_once_with(
            adapter.model_id, trust_remote_code=True
        )

    def test_insufficient_metadata_preflight_skips_checkpoint_load(self) -> None:
        with (
            mock.patch.object(
                dino_adapter.DinoV3Adapter,
                "_select_device",
                return_value="cuda",
            ),
            mock.patch.object(dino_adapter, "env_hf_token", return_value=None),
            mock.patch.object(
                torch.cuda,
                "is_bf16_supported",
                return_value=False,
            ),
            mock.patch.object(
                dino_adapter,
                "_preflight_dino_model_metadata",
                side_effect=dino_adapter.InsufficientGpuMemoryError("too small"),
            ),
            mock.patch.object(dino_adapter.AutoModel, "from_pretrained") as load_model,
            self.assertRaises(dino_adapter.InsufficientGpuMemoryError),
        ):
            dino_adapter.DinoV3Adapter(
                model_id="org/dino",
                use_compile=False,
            )

        load_model.assert_not_called()

    def test_cuda_metadata_preflight_runs_before_checkpoint_load(self) -> None:
        events = []
        model = FakeModel()

        def preflight(*_args, **_kwargs):
            events.append("preflight")
            return "resolved-sha"

        def load_model(*_args, **_kwargs):
            events.append("load")
            return model

        with (
            mock.patch.object(
                dino_adapter.DinoV3Adapter,
                "_select_device",
                return_value="cuda",
            ),
            mock.patch.object(dino_adapter, "env_hf_token", return_value=None),
            mock.patch.object(
                torch.cuda,
                "is_bf16_supported",
                return_value=False,
            ),
            mock.patch.object(
                dino_adapter,
                "_preflight_dino_model_metadata",
                side_effect=preflight,
            ),
            mock.patch.object(
                dino_adapter.AutoModel,
                "from_pretrained",
                side_effect=load_model,
            ),
            mock.patch.object(
                dino_adapter.AutoImageProcessor,
                "from_pretrained",
                return_value=mock.Mock(),
            ),
        ):
            dino_adapter.DinoV3Adapter(
                model_id="org/dino",
                use_compile=False,
            )

        self.assertEqual(events, ["preflight", "load"])

    def test_init_cuda_token_dtype_compile_and_processor_fallbacks(self) -> None:
        for bf16_supported, expected_dtype in (
            (True, torch.bfloat16),
            (False, torch.float16),
        ):
            with self.subTest(bf16_supported=bf16_supported):
                model = FakeModel()
                compiled = mock.Mock(name="compiled_model")
                compile_effect = (
                    compiled if bf16_supported else RuntimeError("compile failed")
                )
                with mock.patch.object(
                    dino_adapter.DinoV3Adapter,
                    "_select_device",
                    return_value="cuda",
                ), mock.patch.object(
                    dino_adapter, "env_hf_token", return_value="secret"
                ), mock.patch.object(
                    torch.cuda,
                    "is_bf16_supported",
                    return_value=bf16_supported,
                ), mock.patch.object(
                    dino_adapter.AutoModel, "from_pretrained", return_value=model
                ) as load_model, mock.patch.object(
                    dino_adapter,
                    "_preflight_dino_model_metadata",
                    return_value="resolved-sha",
                ), mock.patch.object(
                    dino_adapter.AutoImageProcessor,
                    "from_pretrained",
                    side_effect=RuntimeError("processor unavailable"),
                ), mock.patch.object(
                    dino_adapter.DinoV3Adapter,
                    "_build_manual_transform",
                    return_value="manual",
                ), mock.patch.object(
                    torch, "compile", side_effect=[compile_effect]
                ) as compile_model:
                    adapter = dino_adapter.DinoV3Adapter(
                        model_id="custom/model",
                        hf_token="cli",
                    )

                load_model.assert_called_once_with(
                    "custom/model",
                    trust_remote_code=True,
                    token="secret",
                    torch_dtype=expected_dtype,
                    revision="resolved-sha",
                )
                compile_model.assert_called_once_with(model, mode="reduce-overhead")
                self.assertIsNone(adapter.processor)
                self.assertEqual(adapter._manual_transform, "manual")
                if bf16_supported:
                    self.assertIs(adapter.model, compiled)
                else:
                    self.assertIs(adapter.model, model)

    def test_init_cuda_can_skip_compile(self) -> None:
        with mock.patch.object(
            dino_adapter.DinoV3Adapter, "_select_device", return_value="cuda"
        ), mock.patch.object(
            dino_adapter, "env_hf_token", return_value=None
        ), mock.patch.object(
            torch.cuda, "is_bf16_supported", return_value=False
        ), mock.patch.object(
            dino_adapter.AutoModel, "from_pretrained", return_value=FakeModel()
        ), mock.patch.object(
            dino_adapter,
            "_preflight_dino_model_metadata",
            return_value="resolved-sha",
        ), mock.patch.object(
            dino_adapter.AutoImageProcessor,
            "from_pretrained",
            return_value=mock.Mock(),
        ), mock.patch.object(torch, "compile") as compile_model:
            dino_adapter.DinoV3Adapter(
                model_id="test/dino-small",
                use_compile=False,
            )
        compile_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
