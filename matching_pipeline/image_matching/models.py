"""SuperPoint, LightGlue, and classifier adapters for final image verification."""

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from time import perf_counter

import cv2
import joblib
import numpy as np
import torch
from lightglue import LightGlue, SuperPoint
from lightglue.utils import (
    load_image,
    numpy_image_to_torch,
    read_image,
    resize_image,
    rbd,
)

from matching_pipeline.shared.gpu_memory import (
    InsufficientGpuMemoryError,
    log_cuda_memory,
    log_cuda_memory_best_effort,
    module_parameter_buffer_bytes,
    require_cuda_memory,
)

logger = logging.getLogger(__name__)

FEATURE_CACHE_SCHEMA_VERSION = 2
DEFAULT_IMAGE_RESIZE = 720
DEFAULT_MAX_NUM_KEYPOINTS = 1024
_RESIZE_BACKEND = "opencv"
_RESIZE_INTERPOLATION = "area"
_COORDINATE_TRANSFORM_VERSION = "pixel-center-v1"
SUPERPOINT_MODEL_NAME = "lightglue.SuperPoint"
SUPERPOINT_MODEL_VERSION = "superpoint_v1"
_FINGERPRINT_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class PreparedImage:
    """CPU-resized image plus the geometry needed for original coordinates."""

    path: Path
    pixels: np.ndarray
    original_size: tuple[int, int]
    resized_size: tuple[int, int]
    source_signature: tuple[int, int, int, int, int]


def configure_parallel_image_resize() -> None:
    """Avoid multiplying OpenCV's native threads inside our worker pool."""
    cv2.setNumThreads(1)


def prepare_image(
    path: Path,
    *,
    resize: int | None = DEFAULT_IMAGE_RESIZE,
) -> PreparedImage:
    """Decode and resize on CPU before any tensor is transferred to CUDA."""
    path = Path(path)
    initial_signature = _stat_signature(path.stat())
    image = read_image(path)
    original_height, original_width = image.shape[:2]
    if resize is not None:
        image, _scale = resize_image(
            image,
            resize,
            fn="max",
            interp=_RESIZE_INTERPOLATION,
        )
    resized_height, resized_width = image.shape[:2]
    final_signature = _stat_signature(path.stat())
    if final_signature != initial_signature:
        raise RuntimeError(f"Image changed while preparing: {path}")
    return PreparedImage(
        path=path,
        pixels=image,
        original_size=(original_width, original_height),
        resized_size=(resized_width, resized_height),
        source_signature=final_signature,
    )


def _move_model_to_device_with_preflight(
    model: torch.nn.Module,
    device: torch.device | str,
    *,
    component: str,
) -> torch.nn.Module:
    resolved = torch.device(device)
    if resolved.type != "cuda":
        return model.to(device)

    snapshot = log_cuda_memory(
        logger,
        context=f"before {component} model load",
        device=resolved,
    )
    required_bytes = module_parameter_buffer_bytes(model)
    try:
        require_cuda_memory(
            snapshot,
            required_bytes,
            component=component,
            basis="allocator_available",
        )
    except InsufficientGpuMemoryError:
        log_cuda_memory(
            logger,
            context=f"failed {component} memory preflight",
            level=logging.ERROR,
            snapshot=snapshot,
        )
        raise
    logger.info(
        "CUDA memory preflight passed: component=%s model_parameters_and_buffers=%d bytes",
        component,
        required_bytes,
    )
    try:
        return model.to(device)
    except torch.cuda.OutOfMemoryError:
        log_cuda_memory_best_effort(
            logger,
            context=f"OOM loading {component} model",
            device=resolved,
        )
        raise


def _restore_original_image_coordinates(
    features: dict,
    prepared: PreparedImage,
) -> dict:
    result = dict(features)
    keypoints = result.get("keypoints")
    if not isinstance(keypoints, torch.Tensor):
        raise ValueError("SuperPoint output is missing keypoint tensors")
    original_width, original_height = prepared.original_size
    resized_width, resized_height = prepared.resized_size
    scale = keypoints.new_tensor(
        [
            resized_width / original_width,
            resized_height / original_height,
        ]
    )
    result["keypoints"] = (keypoints + 0.5) / scale - 0.5
    result["image_size"] = keypoints.new_tensor(
        [[original_width, original_height]]
    )
    return result


def _package_identity(distribution_name: str) -> tuple[str, str]:
    try:
        package = distribution(distribution_name)
    except PackageNotFoundError:
        return "unknown", "unknown"

    revision = "unknown"
    try:
        direct_url = json.loads(package.read_text("direct_url.json") or "{}")
        vcs_info = direct_url.get("vcs_info", {})
        revision = str(
            vcs_info.get("commit_id")
            or vcs_info.get("requested_revision")
            or "unknown"
        )
    except (AttributeError, TypeError, ValueError):
        pass
    return package.version, revision


LIGHTGLUE_VERSION, LIGHTGLUE_REVISION = _package_identity("lightglue")
KORNIA_VERSION, _ = _package_identity("kornia")
OPENCV_VERSION, _ = _package_identity("opencv-python-headless")


def _inference_device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _source_fingerprint(
    path: Path,
    memo: dict[Path, tuple[tuple[int, int, int, int, int], dict[str, object]]],
) -> dict[str, object]:
    path = Path(path)
    initial_stat = path.stat()
    initial_signature = _stat_signature(initial_stat)
    cached = memo.get(path)
    if cached is not None and cached[0] == initial_signature:
        return cached[1]

    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_FINGERPRINT_CHUNK_SIZE):
            digest.update(chunk)

    final_signature = _stat_signature(path.stat())
    if final_signature != initial_signature:
        raise RuntimeError(f"Image changed while fingerprinting: {path}")

    fingerprint: dict[str, object] = {
        "algorithm": "sha256",
        "digest": digest.hexdigest(),
        "size": initial_stat.st_size,
    }
    memo[path] = (initial_signature, fingerprint)
    return fingerprint


def _json_compatible(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return str(value)


def _tensor_mapping_digest(values: Mapping[str, object]) -> str:
    digest = hashlib.sha256()
    for name in sorted(values):
        tensor = values[name]
        if not isinstance(tensor, torch.Tensor):
            raise ValueError(f"Expected tensor for {name!r}")
        materialized = tensor.detach().cpu().contiguous()
        parts = (
            name.encode("utf-8"),
            str(materialized.dtype).encode("ascii"),
            json.dumps(list(materialized.shape)).encode("ascii"),
            materialized.view(torch.uint8).numpy().tobytes(),
        )
        for part in parts:
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()


def _model_state_fingerprint(model: object) -> str:
    state = model.state_dict()
    if not isinstance(state, Mapping) or not state:
        raise RuntimeError("SuperPoint model has no fingerprintable state")
    return _tensor_mapping_digest(state)


def _validate_features(features: object) -> dict:
    if not isinstance(features, dict):
        raise ValueError("SuperPoint features must be a dictionary")
    required = {"keypoints", "keypoint_scores", "descriptors", "image_size"}
    missing = sorted(required - features.keys())
    if missing:
        raise ValueError(f"SuperPoint features are missing keys: {missing}")
    if any(not isinstance(value, torch.Tensor) for value in features.values()):
        raise ValueError("SuperPoint feature values must all be tensors")

    keypoints = features["keypoints"]
    keypoint_scores = features["keypoint_scores"]
    descriptors = features["descriptors"]
    image_size = features["image_size"]
    if keypoints.ndim != 3 or keypoints.shape[0] != 1 or keypoints.shape[2] != 2:
        raise ValueError(f"Invalid SuperPoint keypoint shape: {keypoints.shape}")
    keypoint_count = keypoints.shape[1]
    if tuple(keypoint_scores.shape) != (1, keypoint_count):
        raise ValueError(
            f"Invalid SuperPoint keypoint-score shape: {keypoint_scores.shape}"
        )
    if (
        descriptors.ndim != 3
        or descriptors.shape[0] != 1
        or descriptors.shape[1] != keypoint_count
        or descriptors.shape[2] <= 0
    ):
        raise ValueError(f"Invalid SuperPoint descriptor shape: {descriptors.shape}")
    if tuple(image_size.shape) != (1, 2):
        raise ValueError(f"Invalid SuperPoint image-size shape: {image_size.shape}")

    for name, value in features.items():
        if value.is_floating_point() and not bool(torch.isfinite(value).all().item()):
            raise ValueError(f"SuperPoint feature {name!r} contains non-finite values")
    if not bool((image_size > 0).all().item()):
        raise ValueError("SuperPoint image size must be positive")
    return features


def _feature_cache_path(base_path: Path, metadata: dict[str, object]) -> Path:
    serialized = json.dumps(
        metadata,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    identity = hashlib.sha256(serialized).hexdigest()
    suffix = base_path.suffix or ".pt"
    stem = base_path.name[: -len(suffix)] if base_path.suffix else base_path.name
    return base_path.with_name(f"{stem}.{identity}{suffix}")


def _load_feature_cache(
    cache_path: Path,
    *,
    expected_metadata: dict[str, object],
    device: torch.device,
) -> dict | None:
    if not cache_path.is_file():
        return None
    try:
        payload = torch.load(
            cache_path,
            map_location=device,
            weights_only=True,
        )
    except torch.cuda.OutOfMemoryError:
        raise
    except Exception:
        logger.warning(
            "Ignoring unreadable SuperPoint feature cache: %s",
            cache_path,
            exc_info=True,
        )
        return None

    if not isinstance(payload, dict):
        logger.warning("Ignoring malformed SuperPoint feature cache: %s", cache_path)
        return None
    if payload.get("metadata") != expected_metadata:
        logger.warning("Ignoring mismatched SuperPoint feature cache: %s", cache_path)
        return None
    features = payload.get("features")
    try:
        validated = _validate_features(features)
        actual_digest = _tensor_mapping_digest(validated)
    except torch.cuda.OutOfMemoryError:
        raise
    except (RuntimeError, TypeError, ValueError):
        logger.warning(
            "Ignoring malformed SuperPoint features: %s",
            cache_path,
            exc_info=True,
        )
        return None
    if payload.get("features_sha256") != actual_digest:
        logger.warning("Ignoring corrupted SuperPoint features: %s", cache_path)
        return None
    return validated


def _write_feature_cache_atomic(
    cache_path: Path,
    *,
    metadata: dict[str, object],
    features: dict,
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{cache_path.name}.tmp.",
        suffix=".pt",
        dir=cache_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        validated = _validate_features(features)
        torch.save(
            {
                "metadata": metadata,
                "features_sha256": _tensor_mapping_digest(validated),
                "features": validated,
            },
            temporary_path,
        )
        with temporary_path.open("rb") as saved_cache:
            os.fsync(saved_cache.fileno())
        os.replace(temporary_path, cache_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


class FeatureExtractor:
    def __init__(
        self,
        max_num_keypoints: int = DEFAULT_MAX_NUM_KEYPOINTS,
    ) -> None:
        started_at = perf_counter()
        self.device = _inference_device()
        self.max_num_keypoints = max_num_keypoints
        self._source_fingerprints: dict[
            Path,
            tuple[tuple[int, int, int, int, int], dict[str, object]],
        ] = {}
        logger.info(
            "Loading SuperPoint feature extractor (max_num_keypoints=%d, device=%s)",
            max_num_keypoints,
            self.device,
        )
        model = SuperPoint(max_num_keypoints=self.max_num_keypoints).eval()
        self.model_configuration = _json_compatible(model.conf)
        self.model_fingerprint = _model_state_fingerprint(model)
        self.model = _move_model_to_device_with_preflight(
            model,
            self.device,
            component="SuperPoint",
        )
        logger.info(
            "SuperPoint feature extractor ready in %.1fs on %s",
            perf_counter() - started_at,
            self.device,
        )

    def extract(
        self,
        path: Path,
        resize: int | None = DEFAULT_IMAGE_RESIZE,
    ) -> dict:
        return self.extract_prepared(prepare_image(path, resize=resize))

    def extract_prepared(self, prepared: PreparedImage) -> dict:
        if _stat_signature(prepared.path.stat()) != prepared.source_signature:
            raise RuntimeError(f"Image changed after preparing: {prepared.path}")
        image = numpy_image_to_torch(prepared.pixels).to(self.device)
        features = self.model.extract(image, resize=None, side="long")
        return _restore_original_image_coordinates(features, prepared)

    def _cache_metadata(
        self,
        *,
        source_fingerprint: dict[str, object],
        resize: int | None,
    ) -> dict[str, object]:
        return {
            "cache_schema_version": FEATURE_CACHE_SCHEMA_VERSION,
            "source": source_fingerprint,
            "extractor": {
                "name": SUPERPOINT_MODEL_NAME,
                "model_version": SUPERPOINT_MODEL_VERSION,
                "model_state_sha256": self.model_fingerprint,
                "configuration": self.model_configuration,
                "lightglue_version": LIGHTGLUE_VERSION,
                "lightglue_revision": LIGHTGLUE_REVISION,
                "torch_version": str(torch.__version__),
                "kornia_version": KORNIA_VERSION,
                "opencv_version": OPENCV_VERSION,
                "max_num_keypoints": self.max_num_keypoints,
                "resize": resize,
                "resize_side": "long",
                "resize_backend": _RESIZE_BACKEND,
                "resize_interpolation": _RESIZE_INTERPOLATION,
                "coordinate_transform_version": _COORDINATE_TRANSFORM_VERSION,
                "device_type": torch.device(self.device).type,
            },
        }

    def _fingerprint(self, image_path: Path) -> dict[str, object]:
        fingerprints = getattr(self, "_source_fingerprints", None)
        if fingerprints is None:
            fingerprints = {}
            self._source_fingerprints = fingerprints
        return _source_fingerprint(image_path, fingerprints)

    def has_compatible_feature_cache(
        self,
        feats_path: Path,
        image_path: Path,
        resize: int | None = DEFAULT_IMAGE_RESIZE,
    ) -> bool:
        """Best-effort check for the exact content/model cache path."""
        try:
            metadata = self._cache_metadata(
                source_fingerprint=self._fingerprint(image_path),
                resize=resize,
            )
            return _feature_cache_path(feats_path, metadata).is_file()
        except (OSError, RuntimeError):
            logger.warning(
                "Could not preflight SuperPoint feature cache; treating as miss: %s",
                image_path,
                exc_info=True,
            )
            return False

    def load_or_extract(
        self,
        feats_path: Path,
        image_path: Path,
        save_missing_feats: bool = True,
        resize: int | None = DEFAULT_IMAGE_RESIZE,
        prepared_image: PreparedImage | Callable[[], PreparedImage] | None = None,
    ) -> dict:
        source_fingerprint = self._fingerprint(image_path)
        metadata = self._cache_metadata(
            source_fingerprint=source_fingerprint,
            resize=resize,
        )
        cache_path = _feature_cache_path(feats_path, metadata)
        feats = _load_feature_cache(
            cache_path,
            expected_metadata=metadata,
            device=self.device,
        )
        if feats is not None:
            if self._fingerprint(image_path) != source_fingerprint:
                raise RuntimeError(f"Image changed while loading cache: {image_path}")
            return feats

        if prepared_image is None:
            extracted = self.extract(image_path, resize=resize)
        else:
            prepared = prepared_image() if callable(prepared_image) else prepared_image
            if prepared.path != Path(image_path):
                raise ValueError(
                    f"Prepared image path {prepared.path} does not match {image_path}"
                )
            extracted = self.extract_prepared(prepared)
        feats = _validate_features(extracted)
        if self._fingerprint(image_path) != source_fingerprint:
            raise RuntimeError(f"Image changed during feature extraction: {image_path}")
        if save_missing_feats:
            _write_feature_cache_atomic(
                cache_path,
                metadata=metadata,
                features=feats,
            )
        return feats


class FeatureMatcher:
    def __init__(self, features: str = "superpoint") -> None:
        started_at = perf_counter()
        self.device = _inference_device()
        logger.info(
            "Loading LightGlue matcher (features=%s, device=%s)", features, self.device
        )
        model = LightGlue(features=features).eval()
        self.model = _move_model_to_device_with_preflight(
            model,
            self.device,
            component="LightGlue",
        )
        logger.info(
            "LightGlue matcher ready in %.1fs on %s",
            perf_counter() - started_at,
            self.device,
        )

    @torch.no_grad()
    def match(self, feats0: dict, feats1: dict) -> dict:
        matches01 = self.model({"image0": feats0, "image1": feats1})
        matches01 = rbd(matches01)
        return matches01


class MatchClassifier:
    def __init__(
        self, model_path: Path = Path(__file__).parent.absolute() / "classifier.pkl"
    ) -> None:
        started_at = perf_counter()
        logger.info("Loading match classifier: %s", model_path)
        self.model = joblib.load(model_path)
        logger.info("Match classifier ready in %.1fs", perf_counter() - started_at)

    def __extract_match_features(self, matches: dict) -> np.ndarray:
        scores = matches["scores"].cpu().numpy()
        if scores.size == 0:
            scores = np.array([0.0])
        return np.array(
            [
                len(scores),
                np.mean(scores),
                np.var(scores),
                np.std(scores),
                np.min(scores),
                np.max(scores),
                np.ptp(scores),
                np.quantile(scores, q=0.1),
                np.quantile(scores, q=0.2),
                np.quantile(scores, q=0.3),
                np.quantile(scores, q=0.4),
                np.quantile(scores, q=0.5),
                np.quantile(scores, q=0.6),
                np.quantile(scores, q=0.7),
                np.quantile(scores, q=0.8),
                np.quantile(scores, q=0.9),
                np.sum(scores),
            ]
        )

    def classify_matches(self, matches: dict) -> tuple[bool, float]:
        features = np.array([self.__extract_match_features(matches)]).reshape(1, -1)
        prediction = self.model.predict(features)[0]
        confidence = self.model.predict_proba(features)[0][1]

        return prediction, confidence
