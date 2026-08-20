"""Shared helpers for metadata LLM runtime setup."""

from __future__ import annotations

import inspect
import logging
import math
import os
from typing import Any

import torch

from matching_pipeline.shared.gpu_memory import (
    InsufficientGpuMemoryError,
    log_cuda_memory,
    log_cuda_memory_best_effort,
    require_cuda_memory,
)
from shared.logging_adapter import LOG_LEVEL_ENV, LogLevel

_VLLM_IMPLICIT_DEVICE_NAMES = {"", "auto", "cuda"}

# These libraries log at INFO (e.g. one line per HTTP request while
# downloading/checking the model from the Hub) and propagate to whatever
# root handler the entrypoint script configures, drowning out our own
# progress output.
_NOISY_LOGGER_NAMES = (
    "httpx",
    "httpcore",
    "urllib3",
    "filelock",
    "huggingface_hub",
)

logger = logging.getLogger(__name__)


def is_debug_enabled() -> bool:
    """Whether the unified log mode permits verbose vLLM diagnostics."""
    return (
        os.getenv(LOG_LEVEL_ENV, LogLevel.ERROR.value).strip().upper()
        == LogLevel.ALL.value
    )


def _quiet_vllm_logging() -> None:
    """Suppress vLLM/HTTP-client noise unless unified logging is set to ALL.

    vLLM normally applies a ``dictConfig`` when imported. Python's dictConfig
    closes every existing handler, including our daily file handler, even
    though vLLM leaves that handler attached to the root logger. Keep logging
    ownership with the entrypoint so vLLM records propagate through the
    already-configured unified handlers instead.

    These environment variables must be set before `vllm` is imported
    anywhere in the process. This module is imported by every stage that later
    does `from vllm import ...`, so setting them here at module load time is
    early enough. The explicit child-logger levels remain effective after the
    shared adapter configures the root handlers.
    """
    os.environ.setdefault("VLLM_CONFIGURE_LOGGING", "0")
    if is_debug_enabled():
        return
    os.environ.setdefault("VLLM_LOGGING_LEVEL", "ERROR")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    # HF_HUB_DISABLE_PROGRESS_BARS only covers huggingface_hub's own bars, not
    # vLLM's native tqdm bars (checkpoint-shard loading, CUDA-graph capture).
    # TQDM_DISABLE is tqdm's own env var and silences those too.
    os.environ.setdefault("TQDM_DISABLE", "1")
    for name in _NOISY_LOGGER_NAMES:
        logging.getLogger(name).setLevel(logging.WARNING)


_quiet_vllm_logging()


def _is_cuda_oom(error: BaseException) -> bool:
    """Recognize direct and chained CUDA OOM failures by stable exception type."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, torch.cuda.OutOfMemoryError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _vllm_cuda_device(device: str) -> str:
    device_name = (device or "").strip().lower()
    return device_name if device_name.startswith("cuda:") else "cuda"


def _log_vllm_oom(error: BaseException, *, context: str, device: str) -> None:
    if _is_cuda_oom(error):
        log_cuda_memory_best_effort(
            logger,
            context=context,
            device=_vllm_cuda_device(device),
        )


def _vllm_engine_accepts_arg(name: str) -> bool:
    """Return whether the installed vLLM EngineArgs accepts ``name``."""
    try:
        from vllm.engine.arg_utils import EngineArgs
    except Exception as exc:
        if _is_cuda_oom(exc):
            raise
        return False

    try:
        parameters = inspect.signature(EngineArgs).parameters
    except (TypeError, ValueError):
        return False

    return name in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _validate_implicit_vllm_device(device: str) -> None:
    device_name = (device or "").strip().lower()
    if device_name in _VLLM_IMPLICIT_DEVICE_NAMES:
        return

    raise ValueError(
        "This installed vLLM version does not accept an explicit `device` "
        f"argument, so METADATA_DEVICE={device!r} cannot be honored. "
        "Use METADATA_DEVICE=cuda/auto, select GPUs with CUDA_VISIBLE_DEVICES, "
        "or set METADATA_BACKEND=transformers for CPU inference."
    )


def _preflight_vllm_gpu_memory(
    *,
    device: str,
    gpu_memory_utilization: float,
) -> None:
    device_name = (device or "").strip().lower()
    if device_name != "cuda" and not device_name.startswith("cuda:"):
        return

    cuda_device = device_name if device_name.startswith("cuda:") else "cuda"
    snapshot = log_cuda_memory(
        logger,
        context="before vLLM model load",
        device=cuda_device,
    )
    required_bytes = math.ceil(snapshot.total_bytes * gpu_memory_utilization)
    try:
        require_cuda_memory(
            snapshot,
            required_bytes,
            component="vLLM",
            basis="driver_free",
        )
    except InsufficientGpuMemoryError:
        log_cuda_memory(
            logger,
            context="failed vLLM memory preflight",
            level=logging.ERROR,
            snapshot=snapshot,
        )
        raise
    logger.info(
        "CUDA memory preflight passed: component=vLLM required=%d bytes "
        "gpu_memory_utilization=%.2f",
        required_bytes,
        gpu_memory_utilization,
    )


def create_vllm(
    *,
    model: str,
    quantization: str | None,
    device: str,
    gpu_memory_utilization: float,
    max_num_seqs: int,
    max_model_len: int,
    trust_remote_code: bool,
) -> Any:
    """Create a vLLM LLM across versions with and without ``device`` support."""
    _preflight_vllm_gpu_memory(
        device=device,
        gpu_memory_utilization=gpu_memory_utilization,
    )

    try:
        # Keep this import after the preflight. Importing vLLM is expensive and
        # can initialize CUDA-related state before the model itself is created.
        from vllm import LLM

        kwargs: dict[str, Any] = {
            "model": model,
            "quantization": quantization,
            "gpu_memory_utilization": gpu_memory_utilization,
            "max_num_seqs": max_num_seqs,
            "max_model_len": max_model_len,
            "trust_remote_code": trust_remote_code,
        }
        if _vllm_engine_accepts_arg("device"):
            kwargs["device"] = device
        else:
            _validate_implicit_vllm_device(device)
        return LLM(**kwargs)
    except Exception as exc:
        _log_vllm_oom(
            exc,
            context="OOM importing or loading vLLM model",
            device=device,
        )
        raise


def run_vllm_generation(
    llm: Any,
    prompts: Any,
    sampling_params: Any,
    *,
    use_tqdm: bool,
    device: str,
) -> Any:
    """Run one vLLM generation call with fatal, diagnostic OOM handling."""
    try:
        return llm.generate(prompts, sampling_params, use_tqdm=use_tqdm)
    except Exception as exc:
        _log_vllm_oom(
            exc,
            context="OOM during vLLM generation",
            device=device,
        )
        raise
