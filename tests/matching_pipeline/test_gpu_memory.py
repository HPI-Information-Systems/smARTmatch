from __future__ import annotations

import logging
import unittest
from unittest import mock

import torch

from matching_pipeline.shared import gpu_memory


class GpuMemoryTests(unittest.TestCase):
    def test_snapshot_reports_driver_and_allocator_memory(self) -> None:
        with (
            mock.patch.object(torch.cuda, "is_available", return_value=True),
            mock.patch.object(torch.cuda, "current_device", return_value=2),
            mock.patch.object(
                torch.cuda,
                "mem_get_info",
                return_value=(6_000, 10_000),
            ) as mem_get_info,
            mock.patch.object(torch.cuda, "memory_allocated", return_value=1_500),
            mock.patch.object(torch.cuda, "memory_reserved", return_value=2_500),
        ):
            snapshot = gpu_memory.cuda_memory_snapshot()

        self.assertEqual(snapshot.device, "cuda:2")
        self.assertEqual(snapshot.free_bytes, 6_000)
        self.assertEqual(snapshot.total_bytes, 10_000)
        self.assertEqual(snapshot.allocated_bytes, 1_500)
        self.assertEqual(snapshot.reserved_bytes, 2_500)
        self.assertEqual(snapshot.allocator_available_bytes, 7_000)
        mem_get_info.assert_called_once_with(torch.device("cuda:2"))

    def test_preflight_uses_requested_memory_basis(self) -> None:
        snapshot = gpu_memory.CudaMemorySnapshot(
            device="cuda:0",
            free_bytes=6_000,
            total_bytes=10_000,
            allocated_bytes=1_500,
            reserved_bytes=2_500,
        )
        gpu_memory.require_cuda_memory(
            snapshot,
            6_500,
            component="model",
            basis="allocator_available",
        )
        with self.assertRaisesRegex(
            gpu_memory.InsufficientGpuMemoryError,
            "component|model|Insufficient CUDA memory",
        ):
            gpu_memory.require_cuda_memory(
                snapshot,
                6_500,
                component="model",
                basis="driver_free",
            )

    def test_memory_log_contains_all_requested_counters(self) -> None:
        snapshot = gpu_memory.CudaMemorySnapshot(
            device="cuda:0",
            free_bytes=1,
            total_bytes=2,
            allocated_bytes=3,
            reserved_bytes=4,
        )
        logger = logging.getLogger("gpu-memory-test")
        with self.assertLogs(logger, level="INFO") as captured:
            gpu_memory.log_cuda_memory(
                logger,
                context="startup",
                snapshot=snapshot,
            )

        output = "\n".join(captured.output)
        for field in ("free", "total", "allocated", "reserved"):
            self.assertIn(f"{field}=", output)
        self.assertIn("context=startup", output)

    def test_module_size_includes_parameters_and_buffers(self) -> None:
        module = torch.nn.Linear(3, 2)
        module.register_buffer("extra", torch.ones(4, dtype=torch.float16))
        expected = sum(
            tensor.numel() * tensor.element_size()
            for tensor in list(module.parameters()) + list(module.buffers())
        )

        self.assertEqual(
            gpu_memory.module_parameter_buffer_bytes(module),
            expected,
        )


if __name__ == "__main__":
    unittest.main()
