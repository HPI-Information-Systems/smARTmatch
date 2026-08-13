"""Base scraper classes

Scraper architecture for this repo:

- Scrapers always persist to Postgres via SQLAlchemy.
- `scrape_url()` returns SQLAlchemy ORM model instance(s) (recommended), OR
    it can directly write via `self.db` and return None.
- No JSON/file outputs are supported at this layer.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from io import BytesIO
from pathlib import Path
import time
from typing import Callable, Iterable, Optional, Sequence, Union

from PIL import Image, ImageOps, UnidentifiedImageError

from scrapers.db_interface import Base, Database
from scrapers.utils.image_storage import repository_root, safe_image_prefix
from scrapers.utils.request_handler import request_image


ModelResult = Union[Base, Sequence[Base]]

_MAX_IMAGE_WIDTH_PX = 2000
_JPEG_QUALITY = 90


def log(prefix: str, message: str) -> None:
    """Module-level log helper for callsites without a Scraper instance.

    Output style: ``[<prefix>] <message>``.  Used by parser/client/helper
    modules that aren't bound to a ``Scraper`` (which has ``self.log``).
    """

    print(f"[{prefix}] {message}")


class Scraper(ABC):
    """Abstract base class for DB-only scrapers.

    Recommended pattern: return model instances and let the base class persist.

        from scrapers.db_interface import Database
        from scrapers.models import AuctionArtwork

        class MyScraper(Scraper):
            def get_urls(self, skip: int) -> list[str]:
                return ["https://example.com/lot/1"]

            def scrape_url(self, url: str):
                return AuctionArtwork(title="Example", lot_url=url)

        with Database() as db:
            MyScraper(db=db).run()

    Alternative pattern: write directly (return None).

        def scrape_url(self, url: str):
            self.db.add(AuctionArtwork(title="Example", lot_url=url))
            return None
    """

    def __init__(
        self,
        db: Optional[Database] = None,
        *,
        log_prefix: str = "scr",
    ):
        """Initialize scraper.

        Args:
            db: Optional Database instance. If omitted, a new Database is created.
            log_prefix: 3-letter platform tag shown in front of every terminal
                line emitted by this scraper (matches the image-filename prefix).
        """
        self.db = db or Database()
        self.stats: dict = {"urls_total": 0, "urls_processed": 0}
        self.log_prefix = log_prefix
        # Optional observers set by the orchestrator. The pre-commit fence
        # prevents writes after a worker loses its cross-process ownership lock;
        # the progress and post-commit observers persist shared run counters.
        self.on_before_batch_commit: Optional[Callable[[], None]] = None
        self.on_progress: Optional[Callable[[], None]] = None
        self.on_batch_commit: Optional[Callable[[], None]] = None

    def _publish_progress(self) -> None:
        """Notify the orchestrator after queue discovery or item completion."""
        if self.on_progress is not None:
            self.on_progress()

    def _commit_batch(self) -> None:
        """Fence ownership, commit pending DB work, then publish progress."""
        if self.on_before_batch_commit is not None:
            self.on_before_batch_commit()
        self.db.commit()
        if self.on_batch_commit is not None:
            self.on_batch_commit()

    def log(self, message: str) -> None:
        """Print one terminal line prefixed with this scraper's 3-letter tag."""

        print(f"[{self.log_prefix}] {message}")

    def run(self, *, skip: int = 0, report_every: int = 10) -> None:
        """Run the scraper and persist results to the database."""

        # Materialize so we can show progress/ETA.
        urls = list(self.get_urls(skip=skip))
        report_every = max(1, int(report_every))
        self.stats = {"urls_total": len(urls), "urls_processed": 0}
        self._publish_progress()

        # If the caller already opened a DB session, reuse it.
        if self.db.session is not None:
            for url in self._iter_urls_with_eta(urls, report_every=report_every):
                self._persist(self.scrape_url(url))
            self._commit_batch()
            return

        # Otherwise manage the session lifecycle here.
        with self.db:
            for url in self._iter_urls_with_eta(urls, report_every=report_every):
                self._persist(self.scrape_url(url))

    def _iter_urls_with_eta(
        self, urls: Sequence[str], *, report_every: int
    ) -> Iterable[str]:
        total = len(urls)
        if total == 0:
            return

        start = time.monotonic()
        for idx, url in enumerate(urls, start=1):
            yield url
            self.stats["urls_processed"] = idx
            self._publish_progress()

            if idx == 1 or idx % report_every == 0 or idx == total:
                elapsed = max(0.0, time.monotonic() - start)
                avg = elapsed / idx
                remaining = max(0, total - idx)
                eta_seconds = remaining * avg
                self.log(
                    f"[progress] {idx}/{total} ({idx / total:.0%}) "
                    f"elapsed={self._fmt_duration(elapsed)} "
                    f"eta={self._fmt_duration(eta_seconds)}"
                )

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        s = int(round(seconds))
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h:d}:{m:02d}:{s:02d}"
        return f"{m:d}:{s:02d}"

    @abstractmethod
    def get_urls(self, skip: int) -> Iterable[str]:
        """Get list of URLs to scrape.

        Args:
            skip: Number of URLs to skip (for pagination/resume).

        Returns:
            List of URLs to scrape.
        """
        pass

    @abstractmethod
    def scrape_url(self, url: str) -> Optional[ModelResult]:
        """Scrape a single URL.

        Args:
            url: The URL to scrape.

        Returns:
            A SQLAlchemy model instance (or list of them) or None.
        """
        pass

    def _persist(self, result: Optional[ModelResult]) -> None:
        """Persist a scrape result to the DB.

        If `scrape_url()` already wrote to the DB and returns None, this is a no-op.
        """

        if result is None:
            return

        # Single instance
        if isinstance(result, Base):
            self.db.add(result)
            return

        # Sequence of instances
        for item in result:
            if not isinstance(item, Base):
                raise TypeError(
                    "scrape_url() must return SQLAlchemy model instance(s) or None"
                )
            self.db.add(item)

    # Shared helper for downloading images to disk so scrapers can store local paths.
    def download_images(
        self,
        image_urls: Sequence[str],
        dest_dir: Union[str, Path],
        prefix: str,
        include_url_hash: bool = True,
    ) -> list[str]:
        dest = Path(dest_dir).expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        file_prefix = safe_image_prefix(prefix)

        local_paths: list[str] = []
        # de-duplicate while preserving order
        seen: set[str] = set()
        deduped_urls: list[str] = []
        for url in image_urls:
            if url in seen:
                continue
            seen.add(url)
            deduped_urls.append(url)

        for idx, url in enumerate(deduped_urls):
            if include_url_hash:
                url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
                filename = f"{file_prefix}_{idx}_{url_hash}.jpg"
            else:
                filename = f"{file_prefix}_{idx}.jpg"
            filepath = dest / filename
            if filepath.exists():
                local_paths.append(_relative_path(filepath))
                continue

            self.log(f"[get image] {url}")
            content = request_image(url, log=self.log)
            if not content:
                self.log(f"[skip] image {url}")
                continue

            jpeg_bytes = _to_jpeg_bytes(content)
            if jpeg_bytes is None:
                self.log(f"[skip] image {url} (invalid/unsupported data)")
                continue

            with open(filepath, "wb") as f:
                f.write(jpeg_bytes)
            local_paths.append(_relative_path(filepath))

        return local_paths


def _to_jpeg_bytes(raw_bytes: bytes) -> bytes | None:
    """Convert image bytes to JPEG and downscale width to <= 2000 px."""

    try:
        with Image.open(BytesIO(raw_bytes)) as image:
            image = ImageOps.exif_transpose(image)

            width, height = image.size
            if width > _MAX_IMAGE_WIDTH_PX and width > 0:
                resized_height = max(1, int(round((height * _MAX_IMAGE_WIDTH_PX) / width)))
                image = image.resize(
                    (_MAX_IMAGE_WIDTH_PX, resized_height),
                    resample=Image.Resampling.LANCZOS,
                )

            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, (255, 255, 255))
                alpha_channel = image.getchannel("A")
                background.paste(image.convert("RGB"), mask=alpha_channel)
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")

            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            return buffer.getvalue()
    except (OSError, UnidentifiedImageError, ValueError):
        return None


def _relative_path(filepath: Path) -> str:
    repo_root = repository_root()
    try:
        return str(filepath.relative_to(repo_root))
    except ValueError:
        return str(filepath)
