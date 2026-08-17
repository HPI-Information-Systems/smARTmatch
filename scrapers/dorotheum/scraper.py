from __future__ import annotations

import os
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy import text

from ..db_interface import Database
from ..utils.auction_helpers import clean_whitespace, fit_varchar, json_dumps
from ..utils.auction_scraper import AuctionPlatformScraper
from ..utils.browser import PlaywrightFetchMixin
from .constants import DEFAULT_CATEGORY_URL
from .entities import (
    build_auction_details,
    resolve_artist_id,
    resolve_default_auctioneer_id,
    resolve_expert_id,
)
from .models import DorotheumListingLot
from .parser import DorotheumLotParser


class DorotheumScraper(PlaywrightFetchMixin, AuctionPlatformScraper):
    """Dorotheum scraper for Gemälde lots."""

    def __init__(
        self,
        *,
        db: Optional[Database] = None,
        category_url: str = DEFAULT_CATEGORY_URL,
        max_pages: Optional[int] = None,
        max_lots: Optional[int] = None,
        min_wait: float = 0.25,
        max_wait: float = 0.75,
        download_images: bool = True,
        images_dir: Optional[str] = None,
        commit_every: int = 20,
        purge: bool = False,
        request_max_retries: int = 5,
        cookie_header: Optional[str] = None,
    ) -> None:
        super().__init__(
            db=db,
            platform_name="Dorotheum",
            min_wait=min_wait,
            max_wait=max_wait,
            purge=purge,
            commit_every=commit_every,
            download_images=download_images,
            images_dir=images_dir,
            module_file=__file__,
        )
        self.category_url = category_url
        self.max_pages = None if max_pages is None else max(1, int(max_pages))
        self.max_lots = None if max_lots is None else max(1, int(max_lots))
        self.request_max_retries = max(1, int(request_max_retries))

        self._parser = DorotheumLotParser()
        self._listing_by_url: dict[str, DorotheumListingLot] = {}
        self._auctioneer_id = None
        self._processed = 0

        self._cookie_header = self._resolve_cookie_header(cookie_header)

    def _prepare_run(self) -> None:
        self._start_browser()
        if self._cookie_header:
            cookies = self._parse_cookie_header(self._cookie_header)
            self._context.add_cookies(
                [
                    {"name": k, "value": v, "domain": ".dorotheum.com", "path": "/"}
                    for k, v in cookies.items()
                ]
            )
        self._auctioneer_id = resolve_default_auctioneer_id(self.db)

    def _after_run(self) -> None:
        self._stop_browser()

    def fetch_html(self, url: str) -> str:
        self.log(f"[get] {url}")
        return self.fetch_html_playwright(url, max_retries=self.request_max_retries)

    def get_urls(self, skip: int = 0) -> Iterable[str]:
        self._listing_by_url = {}
        lot_urls: list[str] = []
        seen: set[str] = set()

        for idx, page_url in enumerate(self._iter_listing_urls(), start=1):
            html = self.fetch_html(page_url)
            if not html:
                continue

            new_count = self._collect_page_lots(html=html, lot_urls=lot_urls, seen=seen)
            total = self.max_pages if self.max_pages is not None else "?"
            self.log(f"[page {idx}/{total}] collected {len(lot_urls)} lot URLs")

            if idx > 1 and new_count == 0:
                break
            if self.max_lots is not None and len(lot_urls) >= self.max_lots + max(
                0, skip
            ):
                break

        if skip:
            lot_urls = lot_urls[skip:]
        if self.max_lots is not None:
            lot_urls = lot_urls[: self.max_lots]

        self.log(f"[discover] {len(lot_urls)} lot URLs")
        return lot_urls

    def scrape_url(self, url: str):
        html = self.fetch_html(url)
        if not html:
            return None

        listing_lot = self._listing_by_url.get(url)
        lot = self._parser.parse_lot_page(
            html=html, lot_url=url, listing_lot=listing_lot
        )
        if lot is None:
            return None

        platform = self.get_platform()
        artist_name = fit_varchar(lot.artist_name)
        artist_id = resolve_artist_id(self.db, artist_name)
        expert_id = resolve_expert_id(self.db, lot)

        # Dorotheum lot numbers can repeat across auctions; prefer stable UID.
        lot_id = fit_varchar(lot.lot_uid) or fit_varchar(lot.lot_id)
        lot_url = clean_whitespace(lot.lot_url)
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

        normalized_title = clean_whitespace(lot.title)

        artwork = self.db.upsert_auction_artwork(
            auction_artwork_id=artwork_id,
            lot_id=lot_id,
            lot_url=lot_url,
            title=normalized_title,
            artist_id=artist_id,
            artist_full_name=artist_name,
            artist_raw_data=(
                json_dumps({"source": "dorotheum", "name": artist_name})
                if artist_name
                else None
            ),
            description=lot.description,
            auction_details=json_dumps(build_auction_details(lot)),
            auction_date=lot.auction_date,
            auction_platform_id=platform.auction_platform_id,
            auctioneer_id=self._auctioneer_id,
            expert_id=expert_id,
            raw_data=lot.raw_data,
        )
        self.set_lot_images(
            artwork_id=artwork.auction_artwork_id,
            image_paths=image_paths,
        )

        if lot_id and (normalized_title is None or artist_id is None):
            self._clear_missing_identity_columns(
                lot_id=lot_id,
                clear_title=normalized_title is None,
                clear_artist=artist_id is None,
            )

        self._processed += 1
        self.log(
            f"[save] lot {lot.lot_id or lot.lot_uid} ({self._processed} processed)"
        )
        return None

    def _clear_missing_identity_columns(
        self, *, lot_id: str, clear_title: bool, clear_artist: bool
    ) -> None:
        columns = self.db._get_table_columns("auction_artwork")
        assignments: list[str] = []

        if clear_title and "title" in columns:
            assignments.append("title = null")

        if clear_artist:
            for column in ("artist_id", "artist_full_name", "artist_raw_data"):
                if column in columns:
                    assignments.append(f"{column} = null")

        if not assignments:
            return

        self.db._get_session().execute(
            text(
                f"update auction_artwork set {', '.join(assignments)} where lot_id = :lot_id"
            ),
            {"lot_id": lot_id},
        )

    def _collect_page_lots(
        self, *, html: str, lot_urls: list[str], seen: set[str]
    ) -> int:
        listing_lots = self._parser.extract_listing_lots(html)
        if listing_lots:
            return self._append_listing_lots(
                listing_lots=listing_lots, lot_urls=lot_urls, seen=seen
            )

        fallback_urls = self._parser.extract_lot_urls(html)
        return self._append_lot_urls(urls=fallback_urls, lot_urls=lot_urls, seen=seen)

    def _append_listing_lots(
        self,
        *,
        listing_lots: list[DorotheumListingLot],
        lot_urls: list[str],
        seen: set[str],
    ) -> int:
        new_count = 0
        for lot in listing_lots:
            if lot.lot_url in seen:
                continue

            seen.add(lot.lot_url)
            lot_urls.append(lot.lot_url)
            self._listing_by_url[lot.lot_url] = lot
            new_count += 1
        return new_count

    @staticmethod
    def _append_lot_urls(
        *, urls: list[str], lot_urls: list[str], seen: set[str]
    ) -> int:
        new_count = 0
        for lot_url in urls:
            if lot_url in seen:
                continue

            seen.add(lot_url)
            lot_urls.append(lot_url)
            new_count += 1
        return new_count

    def _iter_listing_urls(self) -> Iterable[str]:
        yield self.category_url
        if self.max_pages is None or self.max_pages <= 1:
            return

        for page in range(2, self.max_pages + 1):
            yield self._with_page(self.category_url, page)

    @staticmethod
    def _with_page(url: str, page: int) -> str:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params["page"] = [str(page)]
        query = urlencode(params, doseq=True)
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                query,
                parsed.fragment,
            )
        )

    @staticmethod
    def _resolve_cookie_header(cookie_header: Optional[str]) -> str:
        explicit = (cookie_header or "").strip()
        if explicit:
            return explicit
        return (os.getenv("DOROTHEUM_COOKIE_HEADER") or "").strip()

    @staticmethod
    def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
        cookies: dict[str, str] = {}
        for part in cookie_header.split(";"):
            token = part.strip()
            if not token or "=" not in token:
                continue

            name, value = token.split("=", 1)
            name = name.strip()
            value = value.strip()
            if name and value:
                cookies[name] = value

        return cookies
