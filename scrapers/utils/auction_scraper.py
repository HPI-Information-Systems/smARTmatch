from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence
from uuid import UUID, uuid4

from shared.image_storage_lock import image_storage_lock

from ..db_interface import Database
from .auction_helpers import purge_platform_auction_artworks, resolve_images_dir
from .image_storage import platform_image_prefix, safe_image_prefix
from .request_handler import request_html
from .scraper import Scraper


class AuctionPlatformScraper(Scraper):
    """Shared run/purge/request structure for auction-platform scrapers."""

    def __init__(
        self,
        *,
        db: Optional[Database],
        platform_name: str,
        min_wait: float = 0.25,
        max_wait: float = 0.75,
        purge: bool = False,
        commit_every: int = 20,
        download_images: bool = True,
        images_dir: Optional[str] = None,
        module_file: str,
    ) -> None:
        image_prefix = platform_image_prefix(platform_name)
        super().__init__(db=db, log_prefix=image_prefix)
        self.platform_name = platform_name
        self.min_wait = min_wait
        self.max_wait = max_wait
        self.purge_before_run = purge
        self.commit_every = max(1, int(commit_every))
        self.download_images_enabled = download_images
        self.images_dir: Path = resolve_images_dir(
            module_file=module_file,
            images_dir=images_dir,
        )
        self.image_prefix = image_prefix

    def run(self, *, skip: int = 0, report_every: int = 10) -> None:
        with image_storage_lock(self.images_dir, exclusive=False):
            self._run_with_image_storage_locked(
                skip=skip,
                report_every=report_every,
            )

    def _run_with_image_storage_locked(
        self,
        *,
        skip: int,
        report_every: int,
    ) -> None:
        if self.db.session is not None:
            self._run_with_session(skip=skip, report_every=report_every)
            return

        with self.db:
            self._run_with_session(skip=skip, report_every=report_every)

    def _run_with_session(self, *, skip: int, report_every: int) -> None:
        if self.purge_before_run:
            self.purge_existing_data()

        try:
            self._prepare_run()

            urls = list(self.get_urls(skip=skip))
            self.stats = {"urls_total": len(urls), "urls_processed": 0}
            self._publish_progress()

            if not urls:
                self.log("[done] no URLs to scrape")
                return

            for idx, url in enumerate(
                self._iter_urls_with_eta(urls, report_every=max(1, int(report_every))),
                start=1,
            ):
                self.scrape_url(url)
                if idx % self.commit_every == 0:
                    self._commit_batch()

            self._commit_batch()
        finally:
            self._after_run()

    def _prepare_run(self) -> None:
        """Optional hook: called once after purge and before collecting URLs."""

    def _after_run(self) -> None:
        """Optional hook: called once after the scrape loop completes."""

    def fetch_html(self, url: str) -> str:
        self.log(f"[get] {url}")
        html = request_html(
            url,
            min_wait=self.min_wait,
            max_wait=self.max_wait,
            log=self.log,
        )
        return html or ""

    def get_platform(self):
        return self.db.get_or_create_auction_platform(name=self.platform_name)

    def purge_existing_data(self) -> None:
        deleted = purge_platform_auction_artworks(
            self.db, platform_name=self.platform_name
        )
        if deleted == 0:
            self.log("[purge] no existing rows")
            return
        self.log(f"[purge] removed {deleted} existing lots")

    def resolve_storage_artwork_id(
        self,
        *,
        lot_id: str | None,
        lot_url: str | None = None,
        platform_id: UUID | str | None = None,
    ) -> UUID:
        """Return stable DB UUID used in image filenames for one lot."""

        existing = self.db.find_auction_artwork_by_lot(
            lot_id=lot_id,
            lot_url=lot_url,
            auction_platform_id=platform_id,
        )
        existing_id = getattr(existing, "auction_artwork_id", None)

        if isinstance(existing_id, UUID):
            return existing_id

        if existing_id is not None:
            try:
                return UUID(str(existing_id))
            except (TypeError, ValueError):
                pass

        return uuid4()

    def download_lot_images(
        self,
        image_urls: Sequence[str],
        *,
        lot_id: str | None,
        lot_url: str | None = None,
        artwork_id: UUID | None = None,
    ) -> list[str]:
        if not self.download_images_enabled or not image_urls:
            return []

        storage_id = artwork_id or self.resolve_storage_artwork_id(
            lot_id=lot_id,
            lot_url=lot_url,
        )

        try:
            prefix = safe_image_prefix(self.image_prefix, storage_id)
            return self.download_images(
                image_urls,
                self.images_dir,
                prefix,
                include_url_hash=False,
            )
        except Exception as exc:
            lot_display = lot_id or lot_url or "unknown"
            self.log(f"[fail] images for lot {lot_display}: {exc}")
            return []
