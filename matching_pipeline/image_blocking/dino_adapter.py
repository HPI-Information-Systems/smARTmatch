"""Blocking-local DINOv3 embedding adapter."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import AbstractSet, Optional, Protocol

import numpy as np
import torch
from PIL import Image
from transformers import AutoImageProcessor, AutoModel

from matching_pipeline.shared.env import (
    env_dinov3_model_id,
    env_hf_token,
    env_non_gpu_inference_allowed,
)

TOKEN_POOLING_MODES = frozenset({"avg", "median", "min", "max"})
POOLING_ALIASES = {"average": "avg", "mean": "avg"}
# Pillow selects these decoders from the file signature, not the filename suffix.
# Keep extensionless SPSG image paths supported while excluding other parsers.
_INPUT_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
logger = logging.getLogger(__name__)


class ImageGeometry(Protocol):
    def apply(
        self,
        image: Image.Image,
        target_size: tuple[int, int] | None = None,
        pad_color: tuple[int, int, int] = (0, 0, 0),
    ) -> Image.Image:
        """Return an image transformed for model input."""


def pad_color_from_mean(mean: tuple[float, float, float]) -> tuple[int, int, int]:
    color = []
    for value in mean:
        scaled = value * 255 if 0.0 <= value <= 1.0 else value
        color.append(int(max(0, min(255, round(scaled)))))
    return tuple(color)


def normalize_pooling_mode(mode: str) -> str:
    value = str(mode).strip().lower()
    if not value:
        raise ValueError("Pooling mode must not be empty")
    return POOLING_ALIASES.get(value, value)


def normalize_pooling_modes(
    pooling: Sequence[str] | str,
    supported_poolings: AbstractSet[str],
) -> tuple[str, ...]:
    values = [pooling] if isinstance(pooling, str) else list(pooling)
    modes = [normalize_pooling_mode(mode) for mode in values]
    if not modes:
        raise ValueError("At least one pooling mode must be supplied")
    unsupported = [mode for mode in modes if mode not in supported_poolings]
    if unsupported:
        raise ValueError(
            f"Unsupported pooling mode(s): {', '.join(unsupported)}. "
            f"Expected one of: {', '.join(sorted(supported_poolings))}"
        )
    return tuple(dict.fromkeys(modes))


def aggregate_tokens(
    tokens: torch.Tensor,
    mode: str,
    token_mask: torch.Tensor | None = None,
) -> torch.Tensor:
    if tokens.ndim != 3:
        raise ValueError(
            "Expected tokens with shape [batch, tokens, dim], "
            f"got {tuple(tokens.shape)}"
        )
    if tokens.shape[1] <= 0:
        raise ValueError("Cannot aggregate an empty token sequence")
    normalized = normalize_pooling_mode(mode)
    if normalized not in TOKEN_POOLING_MODES:
        raise ValueError(f"Unsupported token pooling mode: {mode!r}")
    if token_mask is None:
        return _aggregate_unmasked_tokens(tokens, normalized)
    return _aggregate_masked_tokens(tokens, normalized, token_mask)


def _aggregate_unmasked_tokens(tokens: torch.Tensor, mode: str) -> torch.Tensor:
    if mode == "avg":
        return tokens.mean(dim=1)
    if mode == "median":
        return tokens.median(dim=1).values
    if mode == "min":
        return tokens.min(dim=1).values
    if mode == "max":
        return tokens.max(dim=1).values
    raise AssertionError(f"Unhandled pooling mode: {mode}")


def _aggregate_masked_tokens(
    tokens: torch.Tensor,
    mode: str,
    token_mask: torch.Tensor,
) -> torch.Tensor:
    if token_mask.shape != tokens.shape[:2]:
        raise ValueError(
            "Expected token_mask with shape [batch, tokens], "
            f"got {tuple(token_mask.shape)} for tokens {tuple(tokens.shape)}"
        )
    mask = token_mask.to(device=tokens.device, dtype=torch.bool)
    counts = mask.sum(dim=1)
    if torch.any(counts <= 0):
        raise ValueError("Cannot aggregate token rows with no valid tokens")
    expanded = mask.unsqueeze(-1)
    if mode == "avg":
        return (tokens * expanded.to(dtype=tokens.dtype)).sum(dim=1) / counts.clamp_min(1).unsqueeze(-1)
    if mode == "min":
        fill = torch.finfo(tokens.dtype).max
        return tokens.masked_fill(~expanded, fill).min(dim=1).values
    if mode == "max":
        fill = torch.finfo(tokens.dtype).min
        return tokens.masked_fill(~expanded, fill).max(dim=1).values
    if mode == "median":
        return torch.stack(
            [row[row_mask].median(dim=0).values for row, row_mask in zip(tokens, mask)],
            dim=0,
        )
    raise AssertionError(f"Unhandled pooling mode: {mode}")


class DinoV3Adapter:
    DEFAULT_POOLING = "default"
    SUPPORTED_POOLINGS = frozenset({"default", "cls"}) | TOKEN_POOLING_MODES
    PAD_COLOR = pad_color_from_mean((0.485, 0.456, 0.406))

    def __init__(
        self,
        use_compile: bool = True,
        hf_token: Optional[str] = None,
        geometry: Optional[ImageGeometry] = None,
        model_id: Optional[str] = None,
    ):
        self.model_id = self._resolve_model_id(model_id)
        self.device = self._select_device()
        self.hf_token = env_hf_token(hf_token)
        self.geometry = geometry
        logger.info("Loading %s on %s", self.model_id, self.device)
        if self.hf_token:
            logger.info("Using Hugging Face token for authenticated download/access")

        processor_kwargs = {}
        model_kwargs = {"trust_remote_code": True}
        if self.hf_token:
            processor_kwargs["token"] = self.hf_token
            model_kwargs["token"] = self.hf_token
        if self.device == "cuda":
            model_kwargs["torch_dtype"] = (
                torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
            )

        self.model = AutoModel.from_pretrained(self.model_id, **model_kwargs).to(
            self.device
        )
        self.model.eval()

        self.processor = None
        self._manual_transform = None
        try:
            self.processor = AutoImageProcessor.from_pretrained(
                self.model_id,
                trust_remote_code=True,
                **processor_kwargs,
            )
            logger.info("Loaded DINOv3 AutoImageProcessor")
        except Exception:
            logger.warning(
                "Could not load DINOv3 AutoImageProcessor; "
                "falling back to manual preprocessing",
                exc_info=True,
            )
            self._manual_transform = self._build_manual_transform()

        if use_compile and self.device == "cuda":
            try:
                self.model = torch.compile(self.model, mode="reduce-overhead")
                logger.info("Applied torch.compile() to %s", self.model_id)
            except Exception:
                logger.warning("torch.compile() not available", exc_info=True)

    @staticmethod
    def _resolve_model_id(model_id: Optional[str]) -> str:
        explicit = str(model_id).strip() if model_id is not None else ""
        return explicit or env_dinov3_model_id()

    @staticmethod
    def _select_device() -> str:
        if torch.cuda.is_available():
            return "cuda"
        if not env_non_gpu_inference_allowed():
            raise RuntimeError(
                "CUDA GPU not detected. Set ALLOW_NON_GPU_INFERENCE=1 to allow "
                "MPS/CPU inference fallback."
            )
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            logger.warning(
                "CUDA unavailable; ALLOW_NON_GPU_INFERENCE is set, using MPS"
            )
            return "mps"
        logger.warning(
            "CUDA unavailable; ALLOW_NON_GPU_INFERENCE is set, using CPU"
        )
        return "cpu"

    def _image_size(self) -> int:
        image_size = getattr(self.model.config, "image_size", 518)
        if isinstance(image_size, (tuple, list)) and image_size:
            image_size = image_size[0]
        try:
            return int(image_size)
        except Exception:
            return 518

    def _build_manual_transform(self):
        try:
            from torchvision import transforms
            from torchvision.transforms import InterpolationMode
        except ImportError as exc:
            raise ImportError(
                "Manual DINOv3 preprocessing requires torchvision. Install "
                "torchvision or use a checkpoint with an AutoImageProcessor."
            ) from exc
        image_size = self._image_size()
        logger.info(
            "Manual DINOv3 preprocessing enabled (image_size=%s)", image_size
        )
        return transforms.Compose(
            [
                transforms.Resize(image_size, interpolation=InterpolationMode.BICUBIC),
                transforms.CenterCrop(image_size),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=[0.485, 0.456, 0.406],
                    std=[0.229, 0.224, 0.225],
                ),
            ]
        )

    def _prepare_images(self, images: list[Image.Image]) -> list[Image.Image]:
        if self.geometry is None:
            return images
        target = (self._image_size(), self._image_size())
        return [self.geometry.apply(image, target, self.PAD_COLOR) for image in images]

    def _prepare_inputs(self, images: list[Image.Image]) -> dict:
        prepared = self._prepare_images(images)
        if self.processor is not None:
            batch = self.processor(images=prepared, return_tensors="pt")
            return {
                key: value.to(self.device) if hasattr(value, "to") else value
                for key, value in batch.items()
            }
        pixel_values = torch.stack(
            [self._manual_transform(img) for img in prepared]
        ).to(self.device)
        return {"pixel_values": pixel_values}

    def _normalize_pooling(self, pooling: Sequence[str] | str) -> tuple[str, ...]:
        return normalize_pooling_modes(pooling, self.SUPPORTED_POOLINGS)

    def _patch_token_start(self) -> int:
        register_count = getattr(self.model.config, "num_register_tokens", 0) or 0
        return 1 + int(register_count)

    def _pool_hidden_state(
        self,
        hidden_state: torch.Tensor,
        pooling: Sequence[str] | str,
    ) -> dict[str, torch.Tensor]:
        modes = self._normalize_pooling(pooling)
        patch_tokens = None
        pooled: dict[str, torch.Tensor] = {}
        for mode in modes:
            if mode in {"default", "cls"}:
                embedding = hidden_state[:, 0, :]
            else:
                if patch_tokens is None:
                    patch_tokens = hidden_state[:, self._patch_token_start() :, :]
                embedding = aggregate_tokens(patch_tokens, mode)
            pooled[mode] = embedding / embedding.norm(p=2, dim=-1, keepdim=True)
        return pooled

    @staticmethod
    def _to_numpy_batch(pooled: dict[str, torch.Tensor]) -> dict[str, np.ndarray]:
        return {mode: value.float().cpu().numpy() for mode, value in pooled.items()}

    def generate_embedding(
        self,
        image_path: str,
        pooling: Sequence[str] | str | None = None,
    ):
        image = Image.open(image_path, formats=_INPUT_IMAGE_FORMATS).convert("RGB")
        return self.generate_embedding_from_pil(image, pooling)

    def generate_embedding_from_pil(
        self,
        image: Image.Image,
        pooling: Sequence[str] | str | None = None,
    ):
        embeddings = self.generate_embeddings_batch_from_pil([image], pooling)
        if pooling is None:
            return embeddings[0]
        return {mode: values[0] for mode, values in embeddings.items()}

    def generate_embeddings_batch(
        self,
        image_paths: list[str],
        pooling: Sequence[str] | str | None = None,
    ):
        images = [
            Image.open(path, formats=_INPUT_IMAGE_FORMATS).convert("RGB")
            for path in image_paths
        ]
        return self.generate_embeddings_batch_from_pil(images, pooling)

    def generate_embeddings_batch_from_pil(
        self,
        images: list[Image.Image],
        pooling: Sequence[str] | str | None = None,
    ):
        return_default_array = pooling is None
        modes = (self.DEFAULT_POOLING,) if pooling is None else pooling
        inputs = self._prepare_inputs(images)
        with torch.no_grad():
            outputs = self.model(**inputs)
            pooled = self._pool_hidden_state(outputs.last_hidden_state, modes)
        embeddings = self._to_numpy_batch(pooled)
        if return_default_array:
            return embeddings[self.DEFAULT_POOLING]
        return embeddings

    def get_model_name(self) -> str:
        return self.model_id.replace("/", "_").replace("-", "_")

    def get_dimension(self) -> int:
        return int(self.model.config.hidden_size)
