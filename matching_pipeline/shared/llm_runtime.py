"""Shared helpers for metadata LLM runtime setup."""

from __future__ import annotations

import inspect
import logging
import os
from typing import Any

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
    """Whether verbose vLLM engine/model-loading diagnostics should be shown.

    Mirrors the FRONTEND_DEBUG convention (see frontend/app.py). Defaults to
    off so `docker compose up` only prints our own progress lines instead of
    vLLM's per-request engine internals.
    """
    return os.getenv("METADATA_VLLM_VERBOSE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _quiet_vllm_logging() -> None:
    """Suppress vLLM/HTTP-client noise unless METADATA_VLLM_VERBOSE is set.

    The VLLM_LOGGING_LEVEL env var must be set before `vllm` is imported
    anywhere in the process, since vLLM reads it once when its logger is
    first configured. This module is imported by every script that later
    does `from vllm import ...`, so setting it here at module load time is
    early enough. The explicit logger.setLevel() calls below don't have that
    ordering requirement -- they take effect regardless of when httpx/etc.
    actually start logging, and survive a later logging.basicConfig() call
    elsewhere in the process (e.g. metadata_matcher.py), since basicConfig
    only touches the root logger, not these already-leveled child loggers.
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
    max_model_len: int,
    trust_remote_code: bool,
) -> Any:
    """Create a vLLM LLM across versions with and without ``device`` support."""
    from vllm import LLM

    kwargs: dict[str, Any] = {
        "model": model,
        "quantization": quantization,
        "gpu_memory_utilization": gpu_memory_utilization,
        "max_model_len": max_model_len,
        "trust_remote_code": trust_remote_code,
    }
    if _vllm_engine_accepts_arg("device"):
        kwargs["device"] = device
    else:
        _validate_implicit_vllm_device(device)

    return LLM(**kwargs)
