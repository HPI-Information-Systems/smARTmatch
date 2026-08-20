from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import patch

from matching_pipeline.shared import gpu_memory, llm_runtime


class LlmRuntimeTest(unittest.TestCase):
    def test_unified_logging_remains_owner_when_verbose_vllm_logs_are_enabled(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {"SMARTMATCH_LOG_LEVEL": "ALL"},
            clear=True,
        ):
            llm_runtime._quiet_vllm_logging()

            self.assertEqual(os.environ["VLLM_CONFIGURE_LOGGING"], "0")
            self.assertNotIn("VLLM_LOGGING_LEVEL", os.environ)

    def _fake_vllm_modules(
        self, *, accepts_device: bool
    ) -> tuple[dict[str, types.ModuleType], list[dict]]:
        calls: list[dict] = []
        vllm_module = types.ModuleType("vllm")
        vllm_module.__path__ = []
        engine_module = types.ModuleType("vllm.engine")
        engine_module.__path__ = []
        arg_utils_module = types.ModuleType("vllm.engine.arg_utils")

        class LLM:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        if accepts_device:

            class EngineArgs:
                def __init__(self, model=None, device=None):
                    pass

        else:

            class EngineArgs:
                def __init__(self, model=None):
                    pass

        vllm_module.LLM = LLM
        arg_utils_module.EngineArgs = EngineArgs
        engine_module.arg_utils = arg_utils_module
        vllm_module.engine = engine_module
        return {
            "vllm": vllm_module,
            "vllm.engine": engine_module,
            "vllm.engine.arg_utils": arg_utils_module,
        }, calls

    def test_create_vllm_omits_cuda_device_when_unsupported(self) -> None:
        modules, calls = self._fake_vllm_modules(accepts_device=False)
        with (
            patch.dict(sys.modules, modules),
            patch.object(llm_runtime, "_preflight_vllm_gpu_memory") as preflight,
        ):
            llm_runtime.create_vllm(
                model="model",
                quantization="awq",
                device="cuda",
                gpu_memory_utilization=0.55,
                max_num_seqs=4,
                max_model_len=4096,
                trust_remote_code=True,
            )

        preflight.assert_called_once_with(
            device="cuda",
            gpu_memory_utilization=0.55,
        )
        self.assertEqual(len(calls), 1)
        self.assertNotIn("device", calls[0])
        self.assertEqual(calls[0]["gpu_memory_utilization"], 0.55)
        self.assertEqual(calls[0]["max_num_seqs"], 4)

    def test_vllm_preflight_uses_requested_fraction_of_total_memory(self) -> None:
        snapshot = gpu_memory.CudaMemorySnapshot(
            device="cuda:0",
            free_bytes=8_000,
            total_bytes=10_001,
            allocated_bytes=0,
            reserved_bytes=0,
        )
        with (
            patch.object(
                llm_runtime,
                "log_cuda_memory",
                return_value=snapshot,
            ),
            patch.object(llm_runtime, "require_cuda_memory") as require,
        ):
            llm_runtime._preflight_vllm_gpu_memory(
                device="cuda",
                gpu_memory_utilization=0.55,
            )

        require.assert_called_once_with(
            snapshot,
            5_501,
            component="vLLM",
            basis="driver_free",
        )

    def test_failed_vllm_preflight_relogs_snapshot_at_error(self) -> None:
        snapshot = gpu_memory.CudaMemorySnapshot(
            device="cuda:0",
            free_bytes=1,
            total_bytes=10,
            allocated_bytes=0,
            reserved_bytes=0,
        )
        with (
            patch.object(
                llm_runtime,
                "log_cuda_memory",
                return_value=snapshot,
            ) as memory_log,
            patch.object(
                llm_runtime,
                "require_cuda_memory",
                side_effect=gpu_memory.InsufficientGpuMemoryError("low memory"),
            ),
            self.assertRaises(gpu_memory.InsufficientGpuMemoryError),
        ):
            llm_runtime._preflight_vllm_gpu_memory(
                device="cuda",
                gpu_memory_utilization=0.55,
            )

        self.assertEqual(memory_log.call_count, 2)
        self.assertEqual(
            memory_log.call_args.kwargs["level"], llm_runtime.logging.ERROR
        )
        self.assertIs(memory_log.call_args.kwargs["snapshot"], snapshot)

    def test_vllm_preflight_skips_explicit_cpu_device(self) -> None:
        with patch.object(llm_runtime, "log_cuda_memory") as memory_log:
            llm_runtime._preflight_vllm_gpu_memory(
                device="cpu",
                gpu_memory_utilization=0.55,
            )
        memory_log.assert_not_called()

    def test_vllm_preflight_skips_automatic_device_selection(self) -> None:
        with patch.object(llm_runtime, "log_cuda_memory") as memory_log:
            llm_runtime._preflight_vllm_gpu_memory(
                device="auto",
                gpu_memory_utilization=0.55,
            )
        memory_log.assert_not_called()

    def test_vllm_generation_logs_typed_direct_or_wrapped_cuda_oom(self) -> None:
        wrapped_error = RuntimeError("vLLM worker failed")
        wrapped_error.__cause__ = llm_runtime.torch.cuda.OutOfMemoryError("wrapped")
        for error in (
            llm_runtime.torch.cuda.OutOfMemoryError("direct"),
            wrapped_error,
        ):
            llm = unittest.mock.Mock()
            llm.generate.side_effect = error
            with (
                self.subTest(error=type(error).__name__),
                patch.object(
                    llm_runtime,
                    "log_cuda_memory_best_effort",
                ) as memory_log,
                self.assertRaises(type(error)),
            ):
                llm_runtime.run_vllm_generation(
                    llm,
                    ["prompt"],
                    object(),
                    use_tqdm=False,
                    device="cuda",
                )

            memory_log.assert_called_once_with(
                llm_runtime.logger,
                context="OOM during vLLM generation",
                device="cuda",
            )
            llm.generate.assert_called_once()

    def test_vllm_generation_does_not_classify_message_only_oom(self) -> None:
        llm = unittest.mock.Mock()
        error = RuntimeError("Engine failed: CUDA out of memory")
        llm.generate.side_effect = error
        with (
            patch.object(
                llm_runtime,
                "log_cuda_memory_best_effort",
            ) as memory_log,
            self.assertRaises(RuntimeError),
        ):
            llm_runtime.run_vllm_generation(
                llm,
                ["prompt"],
                object(),
                use_tqdm=False,
                device="cuda",
            )

        memory_log.assert_not_called()

    def test_create_vllm_passes_device_when_supported(self) -> None:
        modules, calls = self._fake_vllm_modules(accepts_device=True)
        with patch.dict(sys.modules, modules):
            llm_runtime.create_vllm(
                model="model",
                quantization=None,
                device="cpu",
                gpu_memory_utilization=0.55,
                max_num_seqs=4,
                max_model_len=4096,
                trust_remote_code=True,
            )

        self.assertEqual(calls[0]["device"], "cpu")

    def test_create_vllm_rejects_unsupported_explicit_device(self) -> None:
        modules, _ = self._fake_vllm_modules(accepts_device=False)
        with patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(ValueError, "METADATA_DEVICE='cpu'"):
                llm_runtime.create_vllm(
                    model="model",
                    quantization=None,
                    device="cpu",
                    gpu_memory_utilization=0.55,
                    max_num_seqs=4,
                    max_model_len=4096,
                    trust_remote_code=True,
                )


if __name__ == "__main__":
    unittest.main()
