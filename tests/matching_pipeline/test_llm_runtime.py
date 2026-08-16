from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from matching_pipeline.shared import llm_runtime


class LlmRuntimeTest(unittest.TestCase):
    def _fake_vllm_modules(self, *, accepts_device: bool) -> tuple[dict[str, types.ModuleType], list[dict]]:
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
        with patch.dict(sys.modules, modules):
            llm_runtime.create_vllm(
                model="model",
                quantization="awq",
                device="cuda",
                gpu_memory_utilization=0.55,
                max_num_seqs=4,
                max_model_len=4096,
                trust_remote_code=True,
            )

        self.assertEqual(len(calls), 1)
        self.assertNotIn("device", calls[0])
        self.assertEqual(calls[0]["gpu_memory_utilization"], 0.55)
        self.assertEqual(calls[0]["max_num_seqs"], 4)

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
