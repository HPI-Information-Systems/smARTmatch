"""Database updates emitted by the image-blocking stage."""

from __future__ import annotations

import logging
from typing import Sequence

from matching_pipeline.shared.db import connect_db

logger = logging.getLogger(__name__)


def mark_image_files_embedded(image_file_ids: Sequence[str]) -> int:
    """Mark image_file rows as embedded after successful DINO blocking."""
    ids = _coerce_image_file_ids(image_file_ids)
    if not ids:
        return 0
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE image_file
                SET is_embedded = true
                WHERE image_file_id = ANY(%s)
                  AND is_embedded = false
                """,
                (ids,),
            )
            updated = max(int(cur.rowcount), 0)
        conn.commit()
    logger.info("Marked %d image_file rows as embedded", updated)
    return updated


def _coerce_image_file_ids(values: Sequence[str]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            raise ValueError("Missing image_file_id")
        try:
            image_file_id = int(text)
        except ValueError as exc:
            raise ValueError(f"image_file_id must be an integer: {text!r}") from exc
        if image_file_id <= 0:
            raise ValueError(f"image_file_id must be positive: {image_file_id}")
        if image_file_id not in seen:
            seen.add(image_file_id)
            result.append(image_file_id)
    return result
