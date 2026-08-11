from __future__ import annotations

import random
import re
import time
from typing import Any, Optional

import requests

from .html_components import extract_chr_components, extract_lot_header_data
from .html_details import extract_html_details

PLACEHOLDER_TITLES = {
    "Auction Calendar - Upcoming Auctions & Events | Christie's",
    ("Auction Calendar - Upcoming Auctions &amp; Events | " "Christie&#39;s"),
    "Private Sales | What's available",
    "Private Sales | What&#39;s available",
}


class ChristiesHTMLScraper:
    def __init__(
        self,
        *,
        base_delay: float = 0.05,
        rotate_after_requests: int = 200,
        user_agents: Optional[list[str]] = None,
        log=print,
    ) -> None:
        # ``log`` is the bound ``Scraper.log`` of the owning scraper.
        self._log = log
        self.base_delay = base_delay
        self.rotate_after_requests = rotate_after_requests
        self.user_agents = user_agents or [
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        ]
        self.session = requests.Session()
        self.request_count = 0

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": random.choice(self.user_agents),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/webp,*/*;q=0.8"
            ),
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "DNT": "1",
        }
        if self.request_count > 0:
            headers["Referer"] = "https://www.christies.com/en/search"
        return headers

    def _apply_delay(self) -> None:
        time.sleep(self.base_delay)
        self.request_count += 1

        if self.request_count % self.rotate_after_requests == 0:
            self.session.close()
            self.session = requests.Session()

    def _is_placeholder_page(self, page_title: Optional[str]) -> bool:
        return bool(page_title and page_title.strip() in PLACEHOLDER_TITLES)

    def _extract_html_details(self, html: str) -> dict[str, Any]:
        return extract_html_details(html)

    def _extract_chr_components(self, html: str) -> Optional[dict[str, Any]]:
        return extract_chr_components(html)

    def _extract_lot_header_data(self, html: str) -> Optional[dict[str, Any]]:
        return extract_lot_header_data(html)

    def fetch_lot_html(self, lot_id: str, *, verbose: bool = False) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        url = self._lot_url_for_id(lot_id)
        html, reason = self._fetch_html(url)
        if not html:
            if verbose and reason:
                self._log(f"[fail] {lot_id}: {reason}")
            return None, reason

        page_title = self._extract_page_title(html)
        if self._is_placeholder_page(page_title):
            reason = f"placeholder_page: {page_title}"
            if verbose:
                self._log(f"[fail] {lot_id}: {reason}")
            return None, reason

        data = self._extract_lot_header_data(html)
        if not data:
            reason = "no_data_extracted"
            if verbose:
                self._log(f"[fail] {lot_id}: {reason}")
            return None, reason


        return data, None

    def fetch_private_item_html(self, item_id: str) -> Optional[dict[str, Any]]:
        url = f"https://www.christies.com/en/private-sales/privateitems/private-item-{item_id}"
        html, _ = self._fetch_html(url)
        if not html:
            return None

        page_title = self._extract_page_title(html)
        if self._is_placeholder_page(page_title):
            return None

        return self._extract_lot_header_data(html)

    def _fetch_html(self, url: str) -> tuple[Optional[str], Optional[str]]:
        self._apply_delay()

        try:
            response = self.session.get(url, headers=self._get_headers(), timeout=30)
            if response.status_code != 200:
                return None, f"HTTP {response.status_code}"
            return response.text, None
        except Exception as exc:
            return None, f"exception: {type(exc).__name__}: {exc}"

    @staticmethod
    def _lot_url_for_id(lot_id: str) -> str:
        if "." in lot_id:
            parts = lot_id.split(".")
            lot_num = ".".join(parts[1:])
            return f"https://onlineonly.christies.com/sso?objectid={lot_id}&lotnumber={lot_num}"
        return f"https://www.christies.com/en/lot/lot-{lot_id}"

    @staticmethod
    def _extract_page_title(html: str) -> Optional[str]:
        title_match = re.search(r"<title>([^<]+)</title>", html)
        return title_match.group(1).strip() if title_match else None
