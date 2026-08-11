"""Helpers for scraper image storage statistics used by the dashboard."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from scrapers.db_interface import (
    AuctionArtwork,
    AuctionArtworkImageFile,
    AuctionPlatform,
    Database,
    ImageFile,
    LostArtworkImageFile,
)

IMAGE_FILE_SUFFIXES = frozenset({
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".avif",
    ".heic",
    ".heif",
})


def human_readable_bytes(num_bytes: int) -> str:
    """Format bytes with binary units for compact UI display."""

    size = max(0, int(num_bytes))
    if size < 1024:
        return f"{size} B"

    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024.0 or unit == "TB":
            return f"{value:.1f} {unit}"

    return f"{value:.1f} TB"


def _iter_image_paths(raw_paths: Any) -> Iterator[str]:
    """Yield normalized DB image path strings from one row value."""

    if raw_paths is None:
        return

    if isinstance(raw_paths, str):
        candidate = raw_paths.strip()
        if candidate:
            yield candidate
        return

    if not isinstance(raw_paths, Iterable):
        return

    for path_value in raw_paths:
        if path_value is None:
            continue
        candidate = str(path_value).strip()
        if candidate:
            yield candidate


def _resolve_image_path_candidates(*, repo_root: Path, image_path: str) -> Iterator[Path]:
    candidate = str(image_path).strip()
    if not candidate:
        return

    path = Path(candidate).expanduser()
    if path.is_absolute():
        yield path.resolve()
        return

    bases = (
        repo_root,
        repo_root / "scraper_dashboard",
        repo_root / "scrapers",
        Path.cwd(),
    )

    seen: set[Path] = set()
    for base in bases:
        resolved = (base / path).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        yield resolved


def _pick_best_existing_path(*, repo_root: Path, image_path: str) -> Path | None:
    first_candidate: Path | None = None
    for candidate in _resolve_image_path_candidates(repo_root=repo_root, image_path=image_path):
        if first_candidate is None:
            first_candidate = candidate
        if candidate.exists() and candidate.is_file():
            return candidate

    return first_candidate


def collect_image_file_stats(*, repo_root: Path, image_paths: Iterable[str]) -> tuple[int, int]:
    """Count image files and sum their size in bytes for DB-referenced paths."""

    unique_paths: set[Path] = set()
    for image_path in image_paths:
        resolved = _pick_best_existing_path(repo_root=repo_root, image_path=image_path)
        if resolved is not None:
            unique_paths.add(resolved)

    image_count = 0
    disk_bytes = 0
    for path in unique_paths:
        if not path.exists() or not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_FILE_SUFFIXES:
            continue

        try:
            disk_bytes += path.stat().st_size
        except OSError:
            continue

        image_count += 1

    return image_count, disk_bytes


def _table_columns(session: Session, table_name: str) -> set[str]:
    rows = session.execute(
        text(
            """
            select column_name
            from information_schema.columns
            where table_schema = current_schema()
              and table_name = :table_name
            """
        ),
        {"table_name": table_name},
    ).all()
    return {column_name for (column_name,) in rows}


def _table_has_columns(session: Session, table_name: str, required: set[str]) -> bool:
    return required.issubset(_table_columns(session, table_name))


def _load_auction_image_paths(*, session: Session, platform_name: str) -> Iterator[str]:
    has_link_table = _table_has_columns(
        session,
        "auction_artwork_image_file",
        {"auction_artwork_id", "image_file_id"},
    )
    has_image_file = _table_has_columns(
        session,
        "image_file",
        {"image_file_id", "file_path"},
    )

    if has_link_table and has_image_file:
        rows = session.execute(
            select(ImageFile.file_path)
            .join(
                AuctionArtworkImageFile,
                AuctionArtworkImageFile.image_file_id == ImageFile.image_file_id,
            )
            .join(
                AuctionArtwork,
                AuctionArtworkImageFile.auction_artwork_id
                == AuctionArtwork.auction_artwork_id,
            )
            .join(
                AuctionPlatform,
                AuctionArtwork.auction_platform_id
                == AuctionPlatform.auction_platform_id,
            )
            .where(AuctionPlatform.name == platform_name)
        ).scalars()

        for file_path in rows:
            yield from _iter_image_paths(file_path)
        return

    if not _table_has_columns(session, "auction_artwork", {"img_paths"}):
        return

    rows = session.execute(
        text(
            """
            select aa.img_paths
            from auction_artwork aa
            join auction_platform ap
              on aa.auction_platform_id = ap.auction_platform_id
            where ap.name = :platform_name
            """
        ),
        {"platform_name": platform_name},
    )

    for (row_paths,) in rows:
        yield from _iter_image_paths(row_paths)


def _load_lost_art_image_paths(*, session: Session) -> Iterator[str]:
    has_link_table = _table_has_columns(
        session,
        "lost_artwork_image_file",
        {"lost_artwork_id", "image_file_id"},
    )
    has_image_file = _table_has_columns(
        session,
        "image_file",
        {"image_file_id", "file_path"},
    )

    if has_link_table and has_image_file:
        rows = session.execute(
            select(ImageFile.file_path)
            .join(
                LostArtworkImageFile,
                LostArtworkImageFile.image_file_id == ImageFile.image_file_id,
            )
        ).scalars()
        for file_path in rows:
            yield from _iter_image_paths(file_path)
        return

    if not _table_has_columns(session, "lost_artwork", {"img_paths"}):
        return

    rows = session.execute(text("select img_paths from lost_artwork"))
    for (row_paths,) in rows:
        yield from _iter_image_paths(row_paths)


def load_scraper_image_paths(*, db: Database, scraper_info: Mapping[str, Any]) -> list[str]:
    """Load all DB image path references for one scraper."""

    table = str(scraper_info.get("table") or "").strip().lower()
    session = db.SessionLocal()

    try:
        if table == "auction":
            platform_name = str(scraper_info.get("platform_name") or "").strip()
            if not platform_name:
                return []
            return list(_load_auction_image_paths(session=session, platform_name=platform_name))

        if table == "lost":
            return list(_load_lost_art_image_paths(session=session))

        return []
    finally:
        session.close()


def build_storage_stats(
    *,
    db: Database,
    repo_root: Path,
    scraper_name: str,
    scraper_info: Mapping[str, Any],
) -> dict[str, Any]:
    """Build serializable image storage stats for one scraper."""

    del scraper_name  # Reserved for future scraper-specific fallback handling.

    try:
        image_paths = load_scraper_image_paths(db=db, scraper_info=scraper_info)
        image_count, disk_bytes = collect_image_file_stats(
            repo_root=repo_root,
            image_paths=image_paths,
        )
    except Exception:
        image_count, disk_bytes = 0, 0

    return {
        "image_file_count": image_count,
        "image_disk_bytes": disk_bytes,
        "image_disk_usage_human": human_readable_bytes(disk_bytes),
    }
