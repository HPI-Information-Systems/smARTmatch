"""Database updates emitted by the image-blocking stage."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from matching_pipeline.shared.db import connect_db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExpectedImageVersion:
    """Database identity of image bytes used by a blocking run."""

    image_file_id: str
    content_version: int


def mark_image_files_embedded(image_versions: Sequence[ExpectedImageVersion]) -> int:
    """Mark rows embedded only while their content versions still match."""
    expected = _coerce_image_versions(image_versions)
    if not expected:
        return 0
    image_file_ids = [image_file_id for image_file_id, _version in expected]
    content_versions = [version for _image_file_id, version in expected]
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH expected(image_file_id, content_version) AS (
                    SELECT *
                    FROM unnest(%s::integer[], %s::bigint[])
                )
                UPDATE image_file image
                SET is_embedded = true
                FROM expected
                WHERE image.image_file_id = expected.image_file_id
                  AND image.content_version = expected.content_version
                  AND image.is_embedded = false
                """,
                (image_file_ids, content_versions),
            )
            updated = max(int(cur.rowcount), 0)
        conn.commit()
    logger.info(
        "Marked %d of %d version-matched image_file rows as embedded",
        updated,
        len(expected),
    )
    return updated


def _coerce_image_versions(
    values: Sequence[ExpectedImageVersion],
) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    seen: dict[int, int] = {}
    for value in values:
        image_file_id = _positive_integer(value.image_file_id, "image_file_id")
        content_version = _positive_integer(value.content_version, "content_version")
        previous = seen.get(image_file_id)
        if previous is not None:
            if previous != content_version:
                raise ValueError(
                    "Conflicting content versions for image_file_id "
                    f"{image_file_id}: {previous} and {content_version}"
                )
            continue
        seen[image_file_id] = content_version
        result.append((image_file_id, content_version))
    return result


def _positive_integer(value: object, field_name: str) -> int:
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValueError(f"Missing {field_name}")
    try:
        number = int(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer: {text!r}") from exc
    if number <= 0:
        raise ValueError(f"{field_name} must be positive: {number}")
    return number
