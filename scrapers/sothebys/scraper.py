from __future__ import annotations

from typing import Iterable, Optional

import requests
from ..db_interface import Database
from ..utils.auction_helpers import clean_whitespace, fit_varchar, json_dumps
from ..utils.auction_scraper import AuctionPlatformScraper
from .client import SothebysClient
from .constants import BASE_CALENDAR_URL
from .entities import (
    build_auction_details,
    resolve_artist_id,
    resolve_default_auctioneer_id,
)
from .listing import get_existing_lot_ids, iter_auction_urls
from .models import AuctionContext
from .parser import SothebysLotParser


class SothebysScraper(AuctionPlatformScraper):
    """Sotheby's scraper with shared run workflow and modular API/parser helpers."""

    def __init__(
        self,
        *,
        db: Optional[Database] = None,
        max_calendar_pages: Optional[int] = None,
        min_wait: float = 0.25,
        max_wait: float = 0.75,
        purge: bool = False,
        images_dir: Optional[str] = None,
        country: str = "DE",
        language: str = "ENGLISH",
        commit_every: int = 20,
        max_lots_per_auction: Optional[int] = None,
    ) -> None:
        super().__init__(
            db=db,
            platform_name="sothebys",
            min_wait=min_wait,
            max_wait=max_wait,
            purge=purge,
            commit_every=commit_every,
            download_images=True,
            images_dir=images_dir,
            module_file=__file__,
        )
        self.max_calendar_pages = max_calendar_pages
        self.country = country
        self.language = language
        self.max_lots_per_auction = max_lots_per_auction

        self._session = requests.Session()
        self._client = SothebysClient(
            session=self._session, min_wait=min_wait, max_wait=max_wait, log=self.log
        )
        self._parser = SothebysLotParser()

        self._platform_id = None
        self._auctioneer_id = None
        self._auction_context_cache: dict[str, Optional[AuctionContext]] = {}
        self._skipped_existing = 0

    def _prepare_run(self) -> None:
        platform = self.get_platform()
        self._platform_id = platform.auction_platform_id
        self._auctioneer_id = resolve_default_auctioneer_id(self.db)

    def get_urls(self, skip: int) -> Iterable[str]:
        auction_urls = list(self._get_auction_urls())
        self.log(f"[discover] {len(auction_urls)} auction pages to scan")

        existing_lot_ids = self._get_existing_lot_ids()
        if existing_lot_ids:
            self.log(f"[skip] {len(existing_lot_ids)} lots already in DB")

        yielded = 0
        self._skipped_existing = 0
        for auction_idx, auction_url in enumerate(auction_urls, start=1):
            context = self._get_auction_context(auction_url)
            if context is None:
                continue

            try:
                lot_ids = self._client.fetch_auction_lot_ids(
                    context.auction_id, language=self.language
                )
            except Exception as exc:
                self.log(f"[lotcards] [fail] {auction_url}: {exc}")
                continue

            if self.max_lots_per_auction is not None:
                lot_ids = lot_ids[: max(0, int(self.max_lots_per_auction))]

            self.log(
                f"[auction {auction_idx}/{len(auction_urls)}] {auction_url} -> {len(lot_ids)} lots"
            )

            for lot_id in lot_ids:
                if lot_id in existing_lot_ids:
                    self._skipped_existing += 1
                    continue

                yielded += 1
                if yielded <= skip:
                    continue

                existing_lot_ids.add(lot_id)
                yield lot_id

        if self._skipped_existing:
            self.log(f"[skip] {self._skipped_existing} already-scraped lots")

    def scrape_url(self, url: str):
        lot_response = self._client.fetch_lot_response(
            lot_id=url, country=self.country, language=self.language
        )
        if lot_response is None:
            return None

        lot = self._parser.parse_lot_response(lot_response)
        if lot is None:
            return None

        artist_id = resolve_artist_id(self.db, lot.artist_name)
        lot_id = fit_varchar(lot.lot_id)
        lot_url = clean_whitespace(lot.lot_url)
        artwork_id = self.resolve_storage_artwork_id(
            lot_id=lot_id,
            lot_url=lot_url,
            platform_id=self._platform_id,
        )
        local_images = self.download_lot_images(
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
            title=clean_whitespace(lot.title) or f"Lot {lot.lot_id}",
            artist_id=artist_id,
            artist_full_name=artist_name,
            artist_raw_data=(
                json_dumps({"source": "sothebys", "name": artist_name})
                if artist_name
                else None
            ),
            description=lot.description,
            provenance=lot.provenance,
            literature=lot.literature,
            auction_details=json_dumps(build_auction_details(lot)),
            auction_date=lot.auction_date,
            auction_platform_id=self._platform_id,
            auctioneer_id=self._auctioneer_id,
            raw_data=lot.raw_data,
        )
        self.set_lot_images(
            artwork_id=artwork.auction_artwork_id,
            image_paths=local_images,
        )

        self.log(f"[save] lot {lot.lot_id} with {len(local_images)} image(s)")
        return None

    def _get_auction_urls(self) -> Iterable[str]:
        return iter_auction_urls(
            client=self._client,
            base_calendar_url=BASE_CALENDAR_URL,
            max_calendar_pages=self.max_calendar_pages,
        )

    def _get_auction_context(self, auction_url: str) -> Optional[AuctionContext]:
        if auction_url in self._auction_context_cache:
            return self._auction_context_cache[auction_url]

        context = self._client.fetch_auction_context(auction_url)
        self._auction_context_cache[auction_url] = context
        return context

    def _get_existing_lot_ids(self) -> set[str]:
        return get_existing_lot_ids(db=self.db, platform_id=self._platform_id)
