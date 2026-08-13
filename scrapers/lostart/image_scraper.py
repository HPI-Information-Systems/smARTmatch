from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests

from shared.logging_adapter import get_logger

try:
    from .generate_user_agent import generate_user_agent
except ImportError:  # pragma: no cover - fallback for direct script usage
    from generate_user_agent import generate_user_agent


logger = get_logger(__name__)


def _log(message: str) -> None:
    """Log with the same platform tag used by ``LostArtScraper``."""
    log = logger.error if "[fail]" in message.lower() else logger.info
    log("[los] %s", message)


try:
    from scrapers.utils.image_storage import default_images_dir, safe_image_prefix
except ImportError:  # pragma: no cover - fallback for direct script usage

    def default_images_dir() -> Path:
        return Path(__file__).resolve().parents[2] / "db" / "images"

    def safe_image_prefix(*parts: object) -> str:
        raw = "_".join(str(part).strip() for part in parts if part is not None)
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-")
        return cleaned or "image"


DATA_DIR = Path(__file__).resolve().parent / "data"
INDEX_FILE = DATA_DIR / "index.csv"
ASSET_DIR = default_images_dir()

SESSION = requests.Session()
_UA = generate_user_agent()
SESSION.headers.update(
    {
        "User-Agent": _UA.ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
)
if _UA.sec_ch_ua:
    SESSION.headers["sec-ch-ua"] = _UA.sec_ch_ua
if _UA.sec_ch_platform:
    SESSION.headers["sec-ch-ua-platform"] = _UA.sec_ch_platform
if _UA.sec_ch_mobile is not None:
    SESSION.headers["sec-ch-ua-mobile"] = _UA.sec_ch_mobile


class GalleryImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []
        self._inside_gallery: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr_dict = dict(attrs)
        classes = (attr_dict.get("class") or "").split()

        if "object-gallery__slide" in classes:
            self._inside_gallery = 1
        elif self._inside_gallery > 0 and tag != "img":
            self._inside_gallery += 1

        if self._inside_gallery > 0 and tag == "img":
            src = (
                attr_dict.get("src")
                or attr_dict.get("data-src")
                or attr_dict.get("data-lazy")
                or attr_dict.get("data-original")
            )
            if src:
                self.sources.append(src)

    def handle_endtag(self, tag: str) -> None:
        if self._inside_gallery > 0:
            self._inside_gallery -= 1

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        self.handle_starttag(tag, attrs)


def safe_identifier(value: str) -> Optional[str]:
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in {"-", "_"})
    if not cleaned:
        return None
    return cleaned


def iter_index_rows(start_row: int = 0) -> Iterable[tuple[int, dict[str, str]]]:
    if not INDEX_FILE.exists():
        raise FileNotFoundError(f"Index file not found: {INDEX_FILE}")

    with INDEX_FILE.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row_number, row in enumerate(reader):
            if row_number < start_row:
                continue
            yield row_number, row


def fetch_gallery_sources(page_url: str) -> list[str]:
    response = SESSION.get(page_url, timeout=30)
    response.raise_for_status()

    parser = GalleryImageParser()
    parser.feed(response.text)
    parser.close()
    resolved: list[str] = []
    seen: set[str] = set()
    for src in parser.sources:
        absolute = urljoin(page_url, src)
        if absolute in seen:
            continue
        seen.add(absolute)
        resolved.append(absolute)
    return resolved


def determine_extension(url: str, response: requests.Response) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix
    if suffix:
        return suffix

    content_type = response.headers.get("Content-Type", "").lower()
    if "jpeg" in content_type:
        return ".jpg"
    if "png" in content_type:
        return ".png"
    if "gif" in content_type:
        return ".gif"
    if "webp" in content_type:
        return ".webp"
    return ".bin"


def _relative_path(filepath: Path) -> str:
    try:
        return str(filepath.relative_to(Path.cwd()))
    except ValueError:
        return str(filepath)


def download_image(image_url: str, dest: Path) -> str:
    response = SESSION.get(image_url, timeout=30)
    response.raise_for_status()

    extension = determine_extension(image_url, response)
    target_path = dest.with_suffix(extension)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with target_path.open("wb") as fh:
        fh.write(response.content)

    return _relative_path(target_path)


def process_row(
    row_number: int,
    row: dict[str, str],
    *,
    retry_delay: float = 2.0,
    max_attempts: int = 3,
    verbose: bool = True,
) -> int:
    """Download gallery images for a single index row."""

    lost_art_id_raw = (row.get("Lost Art ID") or "").strip()
    link = (row.get("Link") or "").strip()

    if not lost_art_id_raw or not link:
        if verbose:
            _log(f"[skip] row {row_number}: missing Lost Art ID or Link")
        return 0

    safe_id = safe_identifier(lost_art_id_raw)
    if not safe_id:
        if verbose:
            _log(f"[skip] row {row_number}: invalid Lost Art ID '{lost_art_id_raw}'")
        return 0

    image_urls: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            image_urls = fetch_gallery_sources(link)
            break
        except requests.RequestException as exc:
            if verbose:
                _log(f"[fail] row {row_number}: fetch page {link} ({exc})")
            if attempt == max_attempts:
                return 0
            time.sleep(retry_delay)

    if not image_urls:
        if verbose:
            _log(f"[skip] row {row_number}: no gallery images at {link}")
        return 0

    assets_saved = 0
    for idx, image_url in enumerate(image_urls):
        url_hash = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:10]
        asset_base = (
            ASSET_DIR / f"{safe_image_prefix('lostart', safe_id)}_{idx}_{url_hash}"
        )
        for attempt in range(1, max_attempts + 1):
            try:
                download_image(image_url, asset_base)
                assets_saved += 1
                break
            except requests.RequestException as exc:
                if verbose:
                    _log(f"[fail] row {row_number}: download {image_url} ({exc})")
                if attempt == max_attempts:
                    break
                time.sleep(retry_delay)

    if verbose:
        _log(f"[save] row {row_number}: {assets_saved} images for Lost Art ID {lost_art_id_raw}")

    return assets_saved


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Lost Art gallery images.")
    parser.add_argument(
        "--start-row",
        type=int,
        default=0,
        help="Row number in index.csv to start from (0-based).",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Maximum number of rows to process (default: all).",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.25,
        help="Seconds to sleep between rows to limit throughput.",
    )
    return parser.parse_args(argv)


def scrape_images(
    *,
    start_row: int = 0,
    max_rows: Optional[int] = None,
    sleep: float = 0.25,
    verbose: bool = True,
) -> int:
    start_row = max(start_row, 0)
    processed = 0
    rows_with_assets = 0

    ASSET_DIR.mkdir(parents=True, exist_ok=True)

    for row_number, row in iter_index_rows(start_row):
        if max_rows is not None and processed >= max_rows:
            break

        saved = process_row(row_number, row, verbose=verbose)
        if saved:
            rows_with_assets += 1
        processed += 1
        if sleep > 0:
            time.sleep(sleep)

    if verbose:
        _log(f"[done] processed {processed} rows, {rows_with_assets} with assets")

    return rows_with_assets


def run_cli(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)
    try:
        scrape_images(
            start_row=args.start_row,
            max_rows=args.max_rows,
            sleep=args.sleep,
        )
    except KeyboardInterrupt:
        sys.exit("Interrupted by user.")


__all__ = [
    "DATA_DIR",
    "INDEX_FILE",
    "ASSET_DIR",
    "scrape_images",
    "run_cli",
]
