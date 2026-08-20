"""Configuration for final image-matching result handling."""

from __future__ import annotations

import os
from pathlib import Path

from matching_pipeline.shared.env import env_cache_dir, env_positive_int, env_str

MATCHING_PROGRAM_NAME = "lightglue_image_matching"
MATCHING_PROGRAM_VERSION = "0.4.0-cpu_resize_sp1024"
MATCHING_IMAGE_RESIZE_WORKERS_ENV = "MATCHING_IMAGE_RESIZE_WORKERS"
DEFAULT_MATCHING_IMAGE_RESIZE_WORKERS = min(4, os.cpu_count() or 1)
MATCHING_PROGRAM_DESCRIPTION = (
    "SuperPoint/LightGlue verification over DINOv3 blocking candidates; "
    "stores best image confidence, final score, and keypoint coordinates."
)


def matching_image_resize_workers_from_env() -> int:
    """Return the validated number of parallel native CPU resize workers."""
    return env_positive_int(
        MATCHING_IMAGE_RESIZE_WORKERS_ENV,
        DEFAULT_MATCHING_IMAGE_RESIZE_WORKERS,
    )


def matching_results_csv_path_from_env() -> Path | None:
    """Return the debug CSV path when MATCHING_WRITE_OUTPUT_CSV=1 is set."""
    value = env_str("MATCHING_WRITE_OUTPUT_CSV")
    if value is None:
        return None
    if value != "1":
        raise ValueError("MATCHING_WRITE_OUTPUT_CSV must be unset or exactly '1'")
    return env_cache_dir() / "matching_results.csv"
