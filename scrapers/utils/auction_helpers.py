from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import select

from ..db_interface import AuctionArtwork, AuctionPlatform, Database
from .image_storage import resolve_images_dir

MAX_VARCHAR_LEN = 255
MAX_PHONE_LEN = 50


def json_dumps(payload: Any) -> str:
    """Stable JSON encoding used across auction scrapers."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def clean_whitespace(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None


def fit_varchar(value: Optional[str], max_len: int = MAX_VARCHAR_LEN) -> Optional[str]:
    """Trim a string to fit current DB varchar limits."""

    cleaned = clean_whitespace(value)
    if cleaned is None:
        return None
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3].rstrip() + "..."


def purge_platform_auction_artworks(db: Database, *, platform_name: str) -> int:
    """Delete auction_artwork rows for a platform.

    Returns the number of deleted rows.
    """

    session = db._get_session()
    platform = session.execute(
        select(AuctionPlatform).where(AuctionPlatform.name == platform_name)
    ).scalar_one_or_none()
    if not platform:
        return 0

    return (
        session.query(AuctionArtwork)
        .filter(AuctionArtwork.auction_platform_id == platform.auction_platform_id)
        .delete(synchronize_session=False)
    )
