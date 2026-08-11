from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

IMAGE_DIR_ENV_VARS = ("SMARTMATCH_IMAGES_DIR",)

_PLATFORM_PREFIX_BY_KEY = {
    "christies": "chr",
    "sothebys": "sot",
    "drouot": "dro",
    "lottissimo": "lot",
    "dorotheum": "dor",
    "lostart": "los",
}


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_images_dir() -> Path:
    """Return the shared local image directory used by scrapers.

    Keep scraper images next to the local DB project directory instead of under
    each individual scraper package. This makes resets simple: clear the DB and
    clear this one directory, then scrape again.
    """

    for env_var in IMAGE_DIR_ENV_VARS:
        configured = os.getenv(env_var)
        if configured:
            return Path(configured).expanduser().resolve()
    return repository_root() / "db" / "images"


def resolve_images_dir(
    *,
    module_file: str | None = None,
    images_dir: Optional[str] = None,
) -> Path:
    """Resolve the destination directory for downloaded scraper images.

    `module_file` is accepted for backwards-compatible callers. Defaults no
    longer depend on the scraper module location; all scrapers share db/data-production/images.
    """

    if images_dir:
        return Path(images_dir).expanduser().resolve()
    return default_images_dir()


def _normalize_platform_key(platform_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", platform_name.lower())


def platform_image_prefix(platform_name: str) -> str:
    """Return a stable short prefix for image filenames per platform."""

    key = _normalize_platform_key(platform_name)
    if key in _PLATFORM_PREFIX_BY_KEY:
        return _PLATFORM_PREFIX_BY_KEY[key]

    fallback = re.sub(r"[^a-z0-9]+", "", key)[:3]
    return fallback or "img"


def safe_image_prefix(*parts: object) -> str:
    raw = "_".join(str(part).strip() for part in parts if part is not None)
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-")
    return cleaned or "image"
