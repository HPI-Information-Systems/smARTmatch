from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import text

from ..db_interface import Auctioneer, Database
from ..utils.auction_helpers import clean_whitespace, fit_varchar, json_dumps
from ..utils.auction_scraper import AuctionPlatformScraper
from ..utils.browser import PlaywrightFetchMixin
from .constants import BASE_URL, GEMAELDE_URL, PAGE_COUNT_RE
from .entities import resolve_artist_id, resolve_auctioneer
from .parser import LottissimoLotParser


class LottissimoScraper(PlaywrightFetchMixin, AuctionPlatformScraper):
    """Lot-tissimo scraper with shared run/persistence workflow."""

    def __init__(
        self,
        *,
        db: Optional[Database] = None,
        max_pages: Optional[int] = None,
        min_wait: float = 0.25,
        max_wait: float = 0.75,
        purge: bool = False,
        images_dir: Optional[str] = None,
        gemaelde_only: bool = False,
        commit_every: int = 20,
    ) -> None:
        super().__init__(
            db=db,
            platform_name="lot-tissimo",
            min_wait=min_wait,
            max_wait=max_wait,
            purge=purge,
            commit_every=commit_every,
            download_images=True,
            images_dir=images_dir,
            module_file=__file__,
        )
        self.max_pages = max_pages
        self.base_url = GEMAELDE_URL if gemaelde_only else BASE_URL
        self._auctioneer_cache: dict[str, Optional[Auctioneer]] = {}
        self._parser = LottissimoLotParser(log=self.log)

    def _prepare_run(self) -> None:
        self._start_browser()

    def _after_run(self) -> None:
        self._stop_browser()

    def fetch_html(self, url: str, wait_for_selector: Optional[str] = None) -> str:
        self.log(f"[get] {url}")
        return self.fetch_html_playwright(url, wait_for_selector=wait_for_selector)

    def get_urls(self, skip: int = 0) -> Iterable[str]:
        page_urls = self._get_page_urls()
        self.log(f"[discover] {len(page_urls)} list pages to scan")

        lot_urls: list[str] = []
        seen: set[str] = set()

        for idx, page_url in enumerate(page_urls, start=1):
            html = self.fetch_html(page_url, wait_for_selector='a[href*="/lot-"]')
            if not html:
                continue

            for lot_url in self._parser.extract_lot_urls(html):
                if lot_url in seen:
                    continue
                seen.add(lot_url)
                lot_urls.append(lot_url)

            self.log(f"[page {idx}/{len(page_urls)}] collected {len(lot_urls)} unique lot URLs so far")

        if skip:
            lot_urls = lot_urls[skip:]

        self.log(f"[discover] {len(lot_urls)} unique lots")
        return lot_urls

    def scrape_url(self, url: str):
        html = self.fetch_html(url)
        if not html:
            return None

        lot = self._parser.parse_lot_page(html=html, lot_url=url)
        if lot is None:
            return None

        platform = self.get_platform()
        artist_id = resolve_artist_id(self.db, lot.artist_name)
        auctioneer = resolve_auctioneer(
            db=self.db,
            parser=self._parser,
            fetch_html=self.fetch_html,
            cache=self._auctioneer_cache,
            auctioneer_url=lot.auctioneer_url,
            fallback_name=lot.auctioneer_name,
        )
        auctioneer_id = auctioneer.auctioneer_id if auctioneer else None

        artist_name = fit_varchar(lot.artist_name)
        lot_id = fit_varchar(lot.lot_id)
        lot_url = clean_whitespace(url)
        artwork_id = self.resolve_storage_artwork_id(
            lot_id=lot_id,
            lot_url=lot_url,
            platform_id=platform.auction_platform_id,
        )
        local_images = self.download_lot_images(
            lot.image_urls,
            lot_id=lot_id,
            lot_url=lot_url,
            artwork_id=artwork_id,
        )

        normalized_title = clean_whitespace(lot.title)
        artwork = self.db.upsert_auction_artwork(
            auction_artwork_id=artwork_id,
            lot_id=lot_id,
            lot_url=lot_url,
            title=normalized_title,
            artist_id=artist_id,
            artist_full_name=artist_name,
            artist_raw_data=(
                json_dumps({"source": "lot-tissimo", "name": artist_name}) if artist_name else None
            ),
            description=lot.description,
            provenance=lot.provenance,
            auction_details=lot.auction_details,
            auction_date=lot.auction_date,
            auction_platform_id=platform.auction_platform_id,
            auctioneer_id=auctioneer_id,
            raw_data=lot.raw_data,
        )
        self.db.set_auction_artwork_images(
            auction_artwork_id=artwork.auction_artwork_id,
            image_paths=local_images,
        )

        if normalized_title is None and lot_id is not None:
            self._clear_title_column(lot_id=lot_id)

        self.log(f"[save] lot {artwork.lot_id or lot.lot_id} with {len(local_images)} images")
        return None

    def _clear_title_column(self, *, lot_id: str) -> None:
        session = self.db._get_session()
        session.execute(
            text("update auction_artwork set title = null where lot_id = :lot_id"),
            {"lot_id": lot_id},
        )

    def _get_page_urls(self) -> list[str]:
        def pages_for(url: str) -> list[str]:
            html = self.fetch_html(url)
            if not html:
                return []

            page_count = 1
            match = PAGE_COUNT_RE.search(html)
            if match:
                try:
                    page_count = int(match.group(1))
                except ValueError:
                    page_count = 1

            if self.max_pages is not None:
                page_count = max(1, min(page_count, self.max_pages))

            return [url] + [f"{url}&page={i}" for i in range(2, page_count + 1)]

        page_urls = pages_for(self.base_url)
        if not page_urls:
            self.log("[fallback] no pages for selected category; using default listings")
            page_urls = pages_for(BASE_URL)

        return page_urls
