"""Configuration helpers for file-backed DINOv3 image blocking."""

from __future__ import annotations

from pathlib import Path

from matching_pipeline.shared.env import (
    env_auction_to_lost_rankings_dir,
    env_image_blocking_dir,
    env_image_root,
    env_positive_int,
)

LOST_ROLE = "lost"
AUCTION_ROLE = "auction"
VALID_ROLES = frozenset({LOST_ROLE, AUCTION_ROLE})

DEFAULT_IMAGE_BATCH_SIZE = 1
DEFAULT_TOP_K = 100
DEFAULT_MATCHING_BATCH_SIZE = 100
DEFAULT_CANDIDATE_SHARD_AUCTION_IMAGES = 1_000
DEFAULT_EMBEDDING_DTYPE = "float16"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_image_root() -> Path:
    return env_image_root()


def blocking_root() -> Path:
    return env_image_blocking_dir()


def lost_embedding_cache_path() -> Path:
    return blocking_root() / LOST_ROLE / "embeddings.npz"


def blocking_input_csv_path() -> Path:
    return blocking_root() / "blocking_input.csv"


def candidate_dir() -> Path:
    return env_auction_to_lost_rankings_dir()


def matching_batch_size_from_env() -> int:
    """Return the auction-artwork batch limit for one image-matching run."""
    return env_positive_int("MATCHING_BATCH_SIZE", DEFAULT_MATCHING_BATCH_SIZE)
