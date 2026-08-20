"""CUDA memory snapshots, diagnostics, and fail-fast allocation checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import torch

_GIB = 1024**3
MemoryBasis = Literal["driver_free", "allocator_available"]


class InsufficientGpuMemoryError(RuntimeError):
    """Raised before model loading when the visible GPU cannot fit the request."""


@dataclass(frozen=True)
class CudaMemorySnapshot:
    """Device-wide free memory plus this process's PyTorch allocator state."""

    device: str
    free_bytes: int
    total_bytes: int
    allocated_bytes: int
    reserved_bytes: int

    @property
    def allocator_available_bytes(self) -> int:
        """Memory PyTorch can use, including its currently unused cached blocks."""
        reusable_reserved = max(self.reserved_bytes - self.allocated_bytes, 0)
        return self.free_bytes + reusable_reserved


def cuda_memory_snapshot(device: torch.device | str | int | None = None) -> CudaMemorySnapshot:
    """Return the current CUDA driver and PyTorch allocator memory counters."""
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available")

    resolved = torch.device("cuda" if device is None else device)
    if resolved.type != "cuda":
        raise ValueError(f"Expected a CUDA device, got {resolved}")
    device_index = (
        torch.cuda.current_device() if resolved.index is None else resolved.index
    )
    canonical_device = torch.device("cuda", device_index)
    free_bytes, total_bytes = torch.cuda.mem_get_info(canonical_device)
    return CudaMemorySnapshot(
        device=str(canonical_device),
        free_bytes=int(free_bytes),
        total_bytes=int(total_bytes),
        allocated_bytes=int(torch.cuda.memory_allocated(canonical_device)),
        reserved_bytes=int(torch.cuda.memory_reserved(canonical_device)),
    )


def log_cuda_memory(
    logger: logging.Logger,
    *,
    context: str,
    level: int = logging.INFO,
    device: torch.device | str | int | None = None,
    snapshot: CudaMemorySnapshot | None = None,
) -> CudaMemorySnapshot:
    """Log all memory counters and return the snapshot that was logged."""
    current = snapshot or cuda_memory_snapshot(device)
    logger.log(
        level,
        "CUDA memory: context=%s device=%s "
        "free=%.2f GiB (%d bytes) total=%.2f GiB (%d bytes) "
        "allocated=%.2f GiB (%d bytes) reserved=%.2f GiB (%d bytes)",
        context,
        current.device,
        current.free_bytes / _GIB,
        current.free_bytes,
        current.total_bytes / _GIB,
        current.total_bytes,
        current.allocated_bytes / _GIB,
        current.allocated_bytes,
        current.reserved_bytes / _GIB,
        current.reserved_bytes,
    )
    return current


def log_cuda_memory_best_effort(
    logger: logging.Logger,
    *,
    context: str,
    level: int = logging.ERROR,
    device: torch.device | str | int | None = None,
) -> None:
    """Log an OOM-time snapshot without replacing the exception being handled."""
    try:
        log_cuda_memory(logger, context=context, level=level, device=device)
    except Exception as exc:
        logger.log(
            level,
            "CUDA memory snapshot unavailable: context=%s error=%s: %s",
            context,
            type(exc).__name__,
            exc,
        )


def require_cuda_memory(
    snapshot: CudaMemorySnapshot,
    required_bytes: int,
    *,
    component: str,
    basis: MemoryBasis,
) -> None:
    """Reject a model load that cannot fit in the selected memory budget."""
    if required_bytes < 0:
        raise ValueError("required_bytes must not be negative")
    if basis == "driver_free":
        available_bytes = snapshot.free_bytes
    elif basis == "allocator_available":
        available_bytes = snapshot.allocator_available_bytes
    else:
        raise ValueError(f"Unsupported CUDA memory basis: {basis!r}")

    if available_bytes < required_bytes:
        raise InsufficientGpuMemoryError(
            f"Insufficient CUDA memory for {component} on {snapshot.device}: "
            f"required={required_bytes / _GIB:.2f} GiB ({required_bytes} bytes), "
            f"available={available_bytes / _GIB:.2f} GiB ({available_bytes} bytes), "
            f"basis={basis}"
        )


def module_parameter_buffer_bytes(module: torch.nn.Module) -> int:
    """Return bytes needed by a module's parameters and registered buffers."""
    tensors = list(module.parameters()) + list(module.buffers())
    return sum(item.numel() * item.element_size() for item in tensors)
