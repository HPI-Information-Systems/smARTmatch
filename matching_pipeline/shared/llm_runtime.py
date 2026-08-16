"""Shared helpers for metadata LLM runtime setup."""

from __future__ import annotations

import inspect
import logging
import os
from typing import Any

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


def is_debug_enabled() -> bool:
    """Whether the unified log mode permits verbose vLLM diagnostics."""
    return (
        os.getenv(LOG_LEVEL_ENV, LogLevel.ERROR.value).strip().upper()
        == LogLevel.ALL.value
    )


def _quiet_vllm_logging() -> None:
    """Suppress vLLM/HTTP-client noise unless unified logging is set to ALL.

    The VLLM_LOGGING_LEVEL env var must be set before `vllm` is imported
    anywhere in the process, since vLLM reads it once when its logger is
    first configured. This module is imported by every stage that later does
    `from vllm import ...`, so setting it here at module load time is early
    enough. The explicit child-logger levels remain effective after the
    shared adapter configures the root handlers.
    """
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


def _vllm_engine_accepts_arg(name: str) -> bool:
    """Return whether the installed vLLM EngineArgs accepts ``name``."""
    try:
        from vllm.engine.arg_utils import EngineArgs
    except Exception:
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
