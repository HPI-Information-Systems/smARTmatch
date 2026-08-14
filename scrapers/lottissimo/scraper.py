from __future__ import annotations

import os
from typing import Iterable, Optional

import requests
from bs4 import BeautifulSoup
from sqlalchemy import text

from ..db_interface import Auctioneer, Database
from ..utils.auction_helpers import clean_whitespace, fit_varchar, json_dumps
from ..utils.auction_scraper import AuctionPlatformScraper
from ..utils.browser import PlaywrightFetchMixin
from ..utils.request_handler import request_html
from ..utils.user_agents import user_agent_pool
from .constants import BASE_URL, GEMAELDE_URL, LOT_LINK_RE, PAGE_COUNT_RE, PARSER
from .entities import resolve_artist_id, resolve_auctioneer
from .parser import LottissimoLotParser


class LottissimoScraper(PlaywrightFetchMixin, AuctionPlatformScraper):
    """Lot-tissimo scraper with shared run/persistence workflow."""

    def __init__(
        self,
        *,
        db: Optional[Database] = None,
        max_pages: Optional[int] = None,
        max_lots: Optional[int] = None,
        min_wait: float = 0.25,
        max_wait: float = 0.75,
        purge: bool = False,
        images_dir: Optional[str] = None,
        gemaelde_only: bool = False,
        download_images: bool = True,
        commit_every: int = 20,
    ) -> None:
        super().__init__(
            db=db,
            platform_name="lot-tissimo",
            min_wait=min_wait,
            max_wait=max_wait,
            purge=purge,
            commit_every=commit_every,
            download_images=download_images,
            images_dir=images_dir,
            module_file=__file__,
        )
        self.max_pages = max_pages
        self.max_lots = None if max_lots is None else max(1, int(max_lots))
        self.base_url = GEMAELDE_URL if gemaelde_only else BASE_URL
        self._auctioneer_cache: dict[str, Optional[Auctioneer]] = {}
        self._listing_html_cache: dict[str, str] = {}
        self._parser = LottissimoLotParser(log=self.log)

        user_agents = user_agent_pool(os.getenv("LOTTISSIMO_USER_AGENT"))
        self._browser_user_agents = tuple(user_agents)
        self._http_profiles = [
            (
                requests.Session(),
                {
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
                },
            )
            for user_agent in user_agents
        ]
        self._active_http_profile = 0

    def _after_run(self) -> None:
        self._stop_browser()

    def fetch_html(self, url: str, wait_for_selector: Optional[str] = None) -> str:
        """Prefer a complete server-rendered page, then fall back to Chromium."""
        self.log(f"[get] {url}")
        profile_count = len(self._http_profiles)
        attempts = min(3, profile_count)
        for offset in range(attempts):
            profile_index = (self._active_http_profile + offset) % profile_count
            session, headers = self._http_profiles[profile_index]
            html = request_html(
                url,
                max_retries=2,
                min_wait=self.min_wait,
                max_wait=self.max_wait,
                log=self.log,
                session=session,
                headers=headers,
                expected_status=200,
            )
            if self._http_page_is_usable(
                url=url,
                html=html or "",
                wait_for_selector=wait_for_selector,
            ):
                self._active_http_profile = (profile_index + 1) % profile_count
                return html or ""

        self.log("[http] incomplete or blocked response; falling back to Playwright")
        return self.fetch_html_playwright(url, wait_for_selector=wait_for_selector)

    def _http_page_is_usable(
        self,
        *,
        url: str,
        html: str,
        wait_for_selector: Optional[str],
    ) -> bool:
        """Reject challenge, soft-error, and partial pages before parsing."""
        if len(html) < 5_000 or "AwsWafIntegration" in html:
            return False

        soup = BeautifulSoup(html, PARSER)
        title = soup.title.get_text(" ", strip=True).casefold() if soup.title else ""
        if "error" in title and not soup.select_one("h1.header-lot-title"):
            return False

        if wait_for_selector:
            try:
                return soup.select_one(wait_for_selector) is not None
            except Exception:
                return False

        if LOT_LINK_RE.search(url):
            metadata = self._parser._extract_datalayer_dict(html)
            return bool(
                soup.select_one("h1.header-lot-title")
                and metadata
                and (metadata.get("lotId") or "").strip()
            )

        return True

    def get_urls(self, skip: int = 0) -> Iterable[str]:
        page_urls = self._get_page_urls()
        self.log(f"[discover] {len(page_urls)} list pages to scan")

        lot_urls: list[str] = []
        seen: set[str] = set()
        target_count = (
            None if self.max_lots is None else self.max_lots + max(0, int(skip))
        )

        for idx, page_url in enumerate(page_urls, start=1):
            html = self._listing_html_cache.pop(page_url, None)
            if html is None:
                html = self.fetch_html(page_url, wait_for_selector='a[href*="/lot-"]')
            if not html:
                continue

            for lot_url in self._parser.extract_lot_urls(html):
                if lot_url in seen:
                    continue
                seen.add(lot_url)
                lot_urls.append(lot_url)
                if target_count is not None and len(lot_urls) >= target_count:
                    break

            self.log(
                f"[page {idx}/{len(page_urls)}] collected {len(lot_urls)} unique lot URLs so far"
            )
            if target_count is not None and len(lot_urls) >= target_count:
                break

        if skip:
            lot_urls = lot_urls[skip:]
        if self.max_lots is not None:
            lot_urls = lot_urls[: self.max_lots]

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
                json_dumps({"source": "lot-tissimo", "name": artist_name})
                if artist_name
                else None
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

        self.log(
            f"[save] lot {artwork.lot_id or lot.lot_id} with {len(local_images)} images"
        )
        return None

    def _clear_title_column(self, *, lot_id: str) -> None:
        session = self.db._get_session()
        session.execute(
            text("update auction_artwork set title = null where lot_id = :lot_id"),
            {"lot_id": lot_id},
        )

    def _get_page_urls(self) -> list[str]:
        self._listing_html_cache.clear()

        def pages_for(url: str) -> list[str]:
            html = self.fetch_html(url, wait_for_selector='a[href*="/lot-"]')
            if not html:
                return []

            self._listing_html_cache[url] = html
            page_count = 1
            match = PAGE_COUNT_RE.search(html)
            if match:
                try:
                    page_count = int(match.group(1))
                except ValueError:
                    page_count = 1

            if self.max_pages is not None:
                page_count = max(1, min(page_count, self.max_pages))

            separator = "&" if "?" in url else "?"
            return [url] + [
                f"{url}{separator}page={i}" for i in range(2, page_count + 1)
            ]

        page_urls = pages_for(self.base_url)
        if not page_urls:
            self.log(
                "[fallback] no pages for selected category; using default listings"
            )
            page_urls = pages_for(BASE_URL)

        return page_urls
