"""Validated process-environment helpers for the deployed image pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DINOV3_MODEL_ID = "facebook/dinov3-vit7b16-pretrain-lvd1689m"
_IMAGE_FILE_ROLES = frozenset({"auction", "lost"})
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def env_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def env_str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def env_required_str(name: str) -> str:
    value = env_str(name)
    if value is None:
        raise ValueError(f"Environment variable {name} is required")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = env_str(name)
    if value is None:
        return default
    return value.lower() in _TRUTHY_ENV_VALUES


def env_int(name: str, default: int | None = None) -> int | None:
    value = env_str(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def env_positive_int(name: str, default: int) -> int:
    value = env_int(name, default)
    assert value is not None
    if value <= 0:
        raise ValueError(f"Environment variable {name} must be a positive integer")
    return value


def env_path(name: str, default: Path | None = None) -> Path | None:
    value = env_str(name)
    if value is None:
        return default
    return Path(value).expanduser().resolve()


def env_cache_dir() -> Path:
    value = env_path("CACHE_DIR")
    return value if value is not None else env_repo_root() / "cache"


def env_image_root() -> Path:
    return Path(env_required_str("SMARTMATCH_IMAGES_DIR")).expanduser().resolve()


def env_image_blocking_dir() -> Path:
    return env_cache_dir() / "image_blocking"


def env_image_files_parquet_path(role: str) -> Path:
    if role not in _IMAGE_FILE_ROLES:
        raise ValueError(f"Invalid image-file artifact role: {role!r}")
    return env_image_blocking_dir() / role / "image_files.parquet"


def env_auction_to_lost_rankings_dir() -> Path:
    return env_image_blocking_dir() / "auction_to_lost_candidates"


def env_hf_token(cli_token: str | None = None) -> str | None:
    return cli_token or env_str("HF_TOKEN")


def env_dinov3_model_id() -> str:
    return env_str("DINOV3_MODEL_ID", DEFAULT_DINOV3_MODEL_ID) or DEFAULT_DINOV3_MODEL_ID


def env_non_gpu_inference_allowed() -> bool:
    return env_bool("ALLOW_NON_GPU_INFERENCE")


DEFAULT_VLLM_MODEL = "Qwen/Qwen2.5-7B-Instruct-AWQ"
DEFAULT_TRANSFORMERS_MODEL = "LiquidAI/LFM2-700M"
SUPPORTED_METADATA_BACKENDS = {"vllm", "transformers"}


@dataclass(frozen=True)
class MetadataModelConfig:
    backend: str
    model: str
    quantization: str | None
    device: str


def get_model_config() -> MetadataModelConfig:
    """Return validated metadata LLM configuration from the environment."""
    backend = os.getenv("METADATA_BACKEND", "vllm").strip().lower()
    if backend not in SUPPORTED_METADATA_BACKENDS:
        raise ValueError(
            f"Unsupported METADATA_BACKEND={backend!r}; "
            f"expected one of {sorted(SUPPORTED_METADATA_BACKENDS)}"
        )
    is_vllm = backend == "vllm"
    model = os.getenv(
        "METADATA_MODEL", DEFAULT_VLLM_MODEL if is_vllm else DEFAULT_TRANSFORMERS_MODEL
    ).strip()
    quantization = os.getenv(
        "METADATA_QUANTIZATION", "awq_marlin" if is_vllm else ""
    ).strip()
    device = os.getenv("METADATA_DEVICE", "cuda" if is_vllm else "cpu").strip().lower()
    if not model:
        raise ValueError("METADATA_MODEL must not be empty")
    if not device:
        raise ValueError("METADATA_DEVICE must not be empty")
    return MetadataModelConfig(
        backend=backend,
        model=model,
        quantization=quantization or None,
        device=device,
    )
