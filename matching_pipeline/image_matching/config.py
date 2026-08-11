"""Configuration for final image-matching result handling."""

from __future__ import annotations

from pathlib import Path

from matching_pipeline.shared.env import env_cache_dir, env_str

MATCHING_PROGRAM_NAME = "lightglue_image_matching"
MATCHING_PROGRAM_VERSION = "0.3.0-confidence_final_visualization"
MATCHING_PROGRAM_DESCRIPTION = (
    "SuperPoint/LightGlue verification over DINOv3 blocking candidates; "
    "stores best image confidence, final score, and keypoint coordinates."
)


def matching_results_csv_path_from_env() -> Path | None:
    """Return the debug CSV path when MATCHING_WRITE_OUTPUT_CSV=1 is set."""
    value = env_str("MATCHING_WRITE_OUTPUT_CSV")
    if value is None:
        return None
    if value != "1":
        raise ValueError("MATCHING_WRITE_OUTPUT_CSV must be unset or exactly '1'")
    return env_cache_dir() / "matching_results.csv"
