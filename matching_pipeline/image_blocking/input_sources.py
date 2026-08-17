"""Load blocking image-file inputs from Postgres or a CSV snapshot."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from matching_pipeline.shared.db import connect_db

from .config import AUCTION_ROLE, LOST_ROLE, VALID_ROLES, default_image_root, repo_root

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImageFileRow:
    file_id: str
    file_path: Path
    is_embedded: bool | None = None
    content_version: int | None = None
    content_sha256: str | None = None

    def as_strings(self) -> tuple[str, str]:
        return self.file_id, str(self.file_path)


def write_image_file_csv(
    csv_path: Path,
    lost_rows: Sequence[ImageFileRow],
    auction_rows: Sequence[ImageFileRow],
) -> Path:
    path = csv_path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Writing image input CSV: %s (lost=%d, auction=%d)",
        path,
        len(lost_rows),
        len(auction_rows),
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "file_id",
                "file_path",
                "role",
                "content_version",
                "content_sha256",
            ],
        )
        writer.writeheader()
        for role, rows in ((LOST_ROLE, lost_rows), (AUCTION_ROLE, auction_rows)):
            for row in rows:
                writer.writerow(
                    {
                        "file_id": row.file_id,
                        "file_path": _csv_file_path(path.parent, row.file_path),
                        "role": role,
                        "content_version": row.content_version or "",
                        "content_sha256": row.content_sha256 or "",
                    }
                )
    return path


def read_image_file_csv(csv_path: Path) -> tuple[list[ImageFileRow], list[ImageFileRow]]:
    path = csv_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Image input CSV not found: {path}")
    logger.info("Reading image input CSV: %s", path)

    grouped: dict[str, list[ImageFileRow]] = {LOST_ROLE: [], AUCTION_ROLE: []}
    seen: dict[str, set[str]] = {LOST_ROLE: set(), AUCTION_ROLE: set()}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        _require_columns(reader.fieldnames)
        for line_number, row in enumerate(reader, start=2):
            role = _clean(row.get("role"))
            file_id = _clean(row.get("file_id"))
            file_path = _resolve_csv_path(path.parent, _clean(row.get("file_path")))
            content_version = _optional_content_version(
                row.get("content_version"),
                location=f"{path}:{line_number}",
            )
            content_sha256 = _optional_content_sha256(
                row.get("content_sha256"),
                location=f"{path}:{line_number}",
            )
            if role not in VALID_ROLES:
                raise ValueError(f"Invalid role {role!r} at {path}:{line_number}")
            if not file_id:
                raise ValueError(f"Missing file_id at {path}:{line_number}")
            if file_id in seen[role]:
                raise ValueError(f"Duplicate {role} file_id at {path}:{line_number}: {file_id}")
            if not file_path.is_file():
                raise FileNotFoundError(
                    f"Image file not found at {path}:{line_number}: {file_path}"
                )
            seen[role].add(file_id)
            grouped[role].append(
                ImageFileRow(
                    file_id,
                    file_path,
                    content_version=content_version,
                    content_sha256=content_sha256,
                )
            )
    logger.info(
        "Read image input CSV: %s (lost=%d, auction=%d)",
        path,
        len(grouped[LOST_ROLE]),
        len(grouped[AUCTION_ROLE]),
    )
    return grouped[LOST_ROLE], grouped[AUCTION_ROLE]


def reset_auction_image_matching_for_replay() -> tuple[int, int]:
    """Make every auction image pending before an explicit processed-image replay."""

    conn = connect_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auction_artwork_image_file link
                SET is_image_matching_processed = false,
                    is_image_matching_completed_without_error = false
                FROM image_file image
                WHERE image.image_file_id = link.image_file_id
                  AND image.cleaned_up_at IS NULL
                  AND image.file_path IS NOT NULL
                  AND (
                      link.is_image_matching_processed = true
                      OR link.is_image_matching_completed_without_error = true
                  )
                """
            )
            link_count = int(cur.rowcount or 0)
            cur.execute(
                """
                UPDATE auction_artwork artwork
                SET is_image_matching_processed = false,
                    is_image_matching_processed_at = NULL
                WHERE artwork.is_image_matching_processed = true
                  AND EXISTS (
                      SELECT 1
                      FROM auction_artwork_image_file link
                      JOIN image_file image
                        ON image.image_file_id = link.image_file_id
                      WHERE link.auction_artwork_id = artwork.auction_artwork_id
                        AND image.cleaned_up_at IS NULL
                        AND image.file_path IS NOT NULL
                  )
                """
            )
            artwork_count = int(cur.rowcount or 0)
        conn.commit()
        return link_count, artwork_count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def has_unprocessed_auction_image_file_rows() -> bool:
    """Return whether DB-backed blocking has an invalidated or pending auction image."""
    with connect_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM auction_artwork_image_file aaif
                    JOIN image_file img ON img.image_file_id = aaif.image_file_id
                    WHERE img.cleaned_up_at IS NULL
                      AND img.file_path IS NOT NULL
                      AND (
                        img.is_embedded = false
                        OR aaif.is_image_matching_processed = false
                        OR (
                            aaif.is_image_matching_completed_without_error = false
                            AND NOT EXISTS (
                                SELECT 1
                                FROM match_score score
                                WHERE score.auction_id = aaif.auction_artwork_id
                            )
                        )
                    )
                    LIMIT 1
                )
                """
            )
            return bool(cur.fetchone()[0])


def load_db_image_file_rows(
    *,
    lost_limit: int | None = None,
    auction_limit: int | None = None,
    include_processed_auction_images: bool = False,
    validate_files: bool = True,
) -> tuple[list[ImageFileRow], list[ImageFileRow]]:
    """Load DB image rows, optionally requiring each resolved local file to exist."""
    logger.info(
        "Loading DB image rows (lost_limit=%s, auction_artwork_limit=%s, include_processed_auction_images=%s, validate_files=%s)",
        lost_limit,
        auction_limit,
        include_processed_auction_images,
        validate_files,
    )
    with connect_db() as conn:
        _require_image_file_path_column(conn)
        lost = _fetch_db_rows(conn, LOST_ROLE, lost_limit, False, validate_files)
        auction = _fetch_db_rows(
            conn,
            AUCTION_ROLE,
            auction_limit,
            include_processed_auction_images,
            validate_files,
        )
    return lost, auction


def _fetch_db_rows(
    conn,
    role: str,
    limit: int | None,
    include_processed_auction_images: bool,
    validate_files: bool,
) -> list[ImageFileRow]:
    if limit is not None and limit <= 0:
        raise ValueError("limits must be positive")
    use_auction_artwork_limit = role == AUCTION_ROLE and limit is not None
    sql = _db_query(
        role,
        include_processed_auction_images,
        use_auction_artwork_limit=use_auction_artwork_limit,
    )
    params: tuple[int, ...] = ()
    if limit is not None:
        if use_auction_artwork_limit:
            params = (limit,)
        else:
            sql += " LIMIT %s"
            params = (limit,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    logger.info("Fetched %d raw %s image rows from DB", len(rows), role)
    image_root = default_image_root()

    ret = []
    for row in rows:
        try:
            ret.append(
                ImageFileRow(
                    row[0],
                    _db_image_file_path(
                        image_root,
                        image_file_id=row[0],
                        raw_file_path=row[1],
                        validate_files=validate_files,
                    ),
                    is_embedded=(bool(row[2]) if len(row) > 2 else None),
                    content_version=(
                        _required_content_version(row[3], image_file_id=row[0])
                        if len(row) > 3
                        else None
                    ),
                    content_sha256=(
                        _optional_content_sha256(
                            row[4], location=f"image_file_id={row[0]}"
                        )
                        if len(row) > 4
                        else None
                    ),
                )
            )
        except ValueError as exc:
            logger.error(
                "Invalid %s image row image_file_id=%s file_path=%r: %s",
                role,
                row[0],
                row[1],
                exc,
            )
            raise

    logger.info("Resolved %d %s image rows from DB", len(ret), role)
    return ret


def _db_query(
    role: str,
    include_processed_auction_images: bool,
    *,
    use_auction_artwork_limit: bool = False,
) -> str:
    if role == LOST_ROLE:
        return """
            SELECT img.image_file_id::text,
                   img.file_path,
                   img.is_embedded,
                   img.content_version,
                   img.content_sha256
            FROM lost_artwork_image_file laif
            JOIN image_file img ON img.image_file_id = laif.image_file_id
            WHERE img.cleaned_up_at IS NULL
              AND img.file_path IS NOT NULL
            GROUP BY img.image_file_id,
                     img.file_path,
                     img.is_embedded,
                     img.content_version,
                     img.content_sha256
            ORDER BY img.image_file_id ASC
        """
    if role == AUCTION_ROLE:
        return _auction_image_file_query(
            include_processed_auction_images,
            use_auction_artwork_limit=use_auction_artwork_limit,
        )
    raise ValueError(f"Unsupported role: {role}")


def _auction_image_file_query(
    include_processed_auction_images: bool,
    *,
    use_auction_artwork_limit: bool,
) -> str:
    image_filter = _auction_image_state_filter(
        include_processed_auction_images,
        image_alias="img",
    )
    if not use_auction_artwork_limit:
        return f"""
            SELECT img.image_file_id::text,
                   img.file_path,
                   img.is_embedded,
                   img.content_version,
                   img.content_sha256
            FROM auction_artwork_image_file aaif
            JOIN image_file img ON img.image_file_id = aaif.image_file_id
            WHERE img.cleaned_up_at IS NULL
              AND img.file_path IS NOT NULL
              {image_filter}
            GROUP BY img.image_file_id,
                     img.file_path,
                     img.is_embedded,
                     img.content_version,
                     img.content_sha256
            ORDER BY img.image_file_id ASC
        """

    selected_image_filter = _auction_image_state_filter(
        include_processed_auction_images,
        image_alias="selected_image",
    )
    return f"""
        WITH selected_auction_artwork AS (
            SELECT aaif.auction_artwork_id
            FROM auction_artwork_image_file aaif
            JOIN image_file selected_image
              ON selected_image.image_file_id = aaif.image_file_id
            WHERE selected_image.cleaned_up_at IS NULL
              AND selected_image.file_path IS NOT NULL
              {selected_image_filter}
            GROUP BY aaif.auction_artwork_id
            ORDER BY aaif.auction_artwork_id ASC
            LIMIT %s
        )
        SELECT img.image_file_id::text,
               img.file_path,
               img.is_embedded,
               img.content_version,
               img.content_sha256
        FROM selected_auction_artwork selected
        JOIN auction_artwork_image_file aaif
          ON aaif.auction_artwork_id = selected.auction_artwork_id
        JOIN image_file img ON img.image_file_id = aaif.image_file_id
        WHERE img.cleaned_up_at IS NULL
          AND img.file_path IS NOT NULL
          {image_filter}
        GROUP BY img.image_file_id,
                 img.file_path,
                 img.is_embedded,
                 img.content_version,
                 img.content_sha256
        ORDER BY img.image_file_id ASC
    """


def _auction_image_state_filter(
    include_processed_auction_images: bool,
    *,
    image_alias: str,
) -> str:
    if include_processed_auction_images:
        return ""
    if image_alias not in {"img", "selected_image"}:
        raise ValueError(f"Unsupported image SQL alias: {image_alias}")
    return f"""AND (
        {image_alias}.is_embedded = false
        OR aaif.is_image_matching_processed = false
        OR (
            aaif.is_image_matching_completed_without_error = false
            AND NOT EXISTS (
                SELECT 1
                FROM match_score score
                WHERE score.auction_id = aaif.auction_artwork_id
            )
        )
    )"""


def _require_image_file_path_column(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'image_file'
                  AND column_name IN (
                      'file_path',
                      'content_version',
                      'content_sha256'
                  )
                GROUP BY table_name
                HAVING count(*) = 3
            )
            """
        )
        if not cur.fetchone()[0]:
            raise RuntimeError(
                "image_file.file_path, content_version, and content_sha256 "
                "are required for image blocking; apply migration 24"
            )


def _db_image_file_path(
    image_root: Path,
    *,
    image_file_id: str,
    raw_file_path: object,
    validate_files: bool,
) -> Path:
    file_path = _clean(raw_file_path)
    if not file_path:
        raise ValueError(
            f"Missing image_file.file_path for image_file_id={image_file_id}, {raw_file_path}"
        )
    path = _resolve_db_file_path(image_root, file_path, validate_files)
    if validate_files and not path.is_file():
        raise FileNotFoundError(f"Image file not found: {path}")
    return path


def _resolve_db_file_path(image_root: Path, raw_path: str, validate_files: bool) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    candidates = _db_file_path_candidates(image_root, path)
    if validate_files:
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return candidates[0]


def _db_file_path_candidates(image_root: Path, path: Path) -> list[Path]:
    repo_candidate = repo_root() / path
    image_candidate = image_root / path
    try:
        image_root_relative = image_root.resolve().relative_to(repo_root())
    except ValueError:
        image_root_relative = None
    if image_root_relative and _path_starts_with(path, image_root_relative):
        return _unique_paths((repo_candidate, image_candidate))
    return _unique_paths((image_candidate, repo_candidate))


def _path_starts_with(path: Path, prefix: Path) -> bool:
    return path.parts[: len(prefix.parts)] == prefix.parts


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.resolve()
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return result


def _require_columns(fieldnames: Iterable[str] | None) -> None:
    required = {"file_id", "file_path", "role"}
    found = set(fieldnames or [])
    missing = sorted(required - found)
    if missing:
        raise ValueError(f"Input CSV is missing columns: {', '.join(missing)}")


def _resolve_csv_path(base_dir: Path, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("Missing file_path in input CSV")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _csv_file_path(csv_dir: Path, file_path: Path) -> str:
    resolved = file_path.expanduser().resolve()
    try:
        return str(resolved.relative_to(csv_dir))
    except ValueError:
        import os

        return os.path.relpath(resolved, csv_dir)


def _required_content_version(value: object, *, image_file_id: object) -> int:
    version = _optional_content_version(
        value,
        location=f"image_file_id={image_file_id}",
    )
    if version is None:
        raise ValueError(
            f"Missing image_file.content_version for image_file_id={image_file_id}"
        )
    return version


def _optional_content_version(value: object, *, location: str) -> int | None:
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    try:
        version = int(text)
    except ValueError as exc:
        raise ValueError(f"Invalid content_version at {location}: {value!r}") from exc
    if version <= 0:
        raise ValueError(f"content_version must be positive at {location}: {version}")
    return version


def _optional_content_sha256(value: object, *, location: str) -> str | None:
    digest = _clean(value).lower()
    if not digest:
        return None
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(
            f"Invalid content_sha256 at {location}: expected 64 hexadecimal characters"
        )
    return digest


def _clean(value: object) -> str:
    return str(value or "").strip()
