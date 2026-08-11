from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from ..db_interface import Database
from ..utils.auction_helpers import clean_whitespace, fit_varchar, json_dumps
from ..utils.auction_scraper import AuctionPlatformScraper
from .constants import DEFAULT_CATEGORY_URL
from .entities import build_auction_details, resolve_artist_id, resolve_auctioneer_id
from .parser import DrouotLotParser


class DrouotScraper(AuctionPlatformScraper):
    """Drouot scraper using a shared auction scraper workflow."""

    def __init__(
        self,
        *,
        db: Optional[Database] = None,
        category_url: str = DEFAULT_CATEGORY_URL,
        start_page: int = 1,
        max_pages: Optional[int] = None,
        min_wait: float = 0.25,
        max_wait: float = 0.75,
        download_images: bool = True,
        images_dir: Optional[str] = None,
        commit_every: int = 20,
        purge: bool = False,
    ) -> None:
        super().__init__(
            db=db,
            platform_name="Drouot",
            min_wait=min_wait,
            max_wait=max_wait,
            purge=purge,
            commit_every=commit_every,
            download_images=download_images,
            images_dir=images_dir,
            module_file=__file__,
        )
        self.category_url = category_url
        self.start_page = max(1, int(start_page))
        self.max_pages = None if max_pages is None else max(1, int(max_pages))
        self._parser = DrouotLotParser(log=self.log)
        self._processed = 0

    def get_urls(self, skip: int = 0) -> Iterable[str]:
        lot_urls: list[str] = []
        seen: set[str] = set()

        for idx, page_url in enumerate(self._iter_page_urls(), start=1):
            html = self.fetch_html(page_url)
            if not html:
                break

            page_lot_urls = list(self._parser.extract_lot_urls(html))
            if not page_lot_urls:
                break

            new_on_page = 0
            for lot_url in page_lot_urls:
                if lot_url in seen:
                    continue
                seen.add(lot_url)
                lot_urls.append(lot_url)
                new_on_page += 1

            total = self.max_pages if self.max_pages is not None else "?"
            self.log(f"[page {idx}/{total}] collected {len(lot_urls)} unique lot URLs so far")

            # Stop if the platform starts repeating already-seen pages.
            if new_on_page == 0:
                break

        if skip:
            lot_urls = lot_urls[skip:]

        self.log(f"[discover] {len(lot_urls)} lot URLs")
        return lot_urls

    def scrape_url(self, url: str):
        html = self.fetch_html(url)
        if not html:
            return None

        lot = self._parser.parse_lot_page(html=html, lot_url=url)
        if lot is None:
            return None

        platform = self.get_platform()
        title = clean_whitespace(lot.title)
        if not title:
            self.log(f"[skip] lot {lot.lot_id}: no reliable title")
            return None

        artist_id = resolve_artist_id(self.db, lot.artist_name)
        auctioneer_id = resolve_auctioneer_id(self.db, lot)
        lot_id = fit_varchar(lot.lot_id)
        lot_url = clean_whitespace(url)
        artwork_id = self.resolve_storage_artwork_id(
            lot_id=lot_id,
            lot_url=lot_url,
            platform_id=platform.auction_platform_id,
        )
        image_paths = self.download_lot_images(
            lot.image_urls,
            lot_id=lot_id,
            lot_url=lot_url,
            artwork_id=artwork_id,
        )

        artist_name = fit_varchar(lot.artist_name)
        artwork = self.db.upsert_auction_artwork(
            auction_artwork_id=artwork_id,
            lot_id=lot_id,
            lot_url=lot_url,
            title=title,
            artist_id=artist_id,
            artist_full_name=artist_name,
            artist_raw_data=(
                json_dumps({"source": "drouot", "name": artist_name}) if artist_name else None
            ),
            description=lot.description,
            auction_details=json_dumps(build_auction_details(lot)),
            auction_date=lot.auction_date,
            auction_platform_id=platform.auction_platform_id,
            auctioneer_id=auctioneer_id,
            raw_data=lot.raw_data,
        )
        self.db.set_auction_artwork_images(
            auction_artwork_id=artwork.auction_artwork_id,
            image_paths=image_paths,
        )

        self._processed += 1
        self.log(f"[save] lot {lot.lot_id} ({self._processed} processed)")
        return None

    def _iter_page_urls(self) -> Iterable[str]:
        page = self.start_page
        yielded = 0
        while self.max_pages is None or yielded < self.max_pages:
            yield self._with_page(self.category_url, page)
            page += 1
            yielded += 1

    @staticmethod
    def _with_page(url: str, page: int) -> str:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params["page"] = [str(page)]
        query = urlencode(params, doseq=True)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, parsed.fragment))

    def _derive_artist_and_title(self, display_title: str, description: str) -> tuple[str, Optional[str]]:
        return self._parser._derive_artist_and_title(display_title, description)

    def _build_raw_payload(
        self,
        *,
        lot_url: str,
        lot_object: str,
        product_schema: Optional[dict[str, object]],
        display_heading: Optional[str],
    ) -> str:
        return self._parser._build_raw_payload(
            lot_url=lot_url,
            lot_object=lot_object,
            product_schema=product_schema,
            display_heading=display_heading,
        )

    def _extract_description_from_dom(self, soup: BeautifulSoup) -> str:
        return self._parser._extract_description_from_dom(soup)
