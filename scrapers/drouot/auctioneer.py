from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .constants import BASE_URL


class DrouotAuctioneerMixin:
    def _extract_auctioneer_name(self, soup: BeautifulSoup, lot_object: str) -> Optional[str]:
        anchor = self._find_auctioneer_anchor(soup)
        if anchor is not None:
            value = anchor.get_text(" ", strip=True)
            if value:
                return value

        return self._extract_js_string(lot_object, "auctioneerName")

    def _extract_auctioneer_url(self, soup: BeautifulSoup) -> Optional[str]:
        anchor = self._find_auctioneer_anchor(soup)
        if not anchor:
            return None
        href = str(anchor.get("href", "")).strip()
        if not href:
            return None
        return urljoin(BASE_URL, href)

    def _extract_auctioneer_phone(self, soup: BeautifulSoup, lot_object: str) -> Optional[str]:
        anchor = self._find_auctioneer_anchor(soup)
        if anchor is not None:
            section = anchor.parent
            if section is not None:
                text = section.get_text(" ", strip=True)
                match = re.search(r"(\+?\d[\d\s]{5,})", text)
                if match:
                    return " ".join(match.group(1).split())

        return self._extract_js_string(lot_object, "telephone")

    def _extract_auctioneer_email(self, soup: BeautifulSoup, lot_object: str) -> Optional[str]:
        anchor = self._find_auctioneer_anchor(soup)
        if anchor is not None:
            section = anchor.parent
            if section is not None:
                email_anchor = section.find("a", href=re.compile(r"^mailto:"))
                if email_anchor:
                    href = str(email_anchor.get("href", ""))
                    if href.startswith("mailto:"):
                        return href.replace("mailto:", "", 1).strip()

        return self._extract_js_string(lot_object, "emailContact")

    def _extract_auctioneer_address(self, soup: BeautifulSoup, lot_object: str) -> Optional[str]:
        address = self._extract_js_string(lot_object, "address")
        city = self._extract_js_string(lot_object, "city")
        zip_code = self._extract_js_string(lot_object, "zipCode")

        parts = [p for p in [address, zip_code, city] if p]
        if parts:
            return ", ".join(parts)

        h2 = self._find_h2_with_text(soup, "Auktion")
        if h2 is not None:
            section = h2.parent
            if section is not None:
                text = section.get_text(" ", strip=True)
                match = re.search(r"([A-Za-zÀ-ÿ\-\s\.'’]+\d+[^|]{8,120})", text)
                if match:
                    return " ".join(match.group(1).split())

        return None

    def _find_h2_with_text(self, soup: BeautifulSoup, needle: str):
        needle_lower = needle.lower()
        for heading in soup.find_all("h2"):
            text = heading.get_text(" ", strip=True)
            if needle_lower in text.lower():
                return heading
        return None

    def _find_auctioneer_anchor(self, soup: BeautifulSoup):
        return soup.find("a", href=re.compile(r"/[a-z]{2}/auctioneer/\d+/"))

    def _extract_catalogue_name(self, soup: BeautifulSoup) -> Optional[str]:
        sale_anchor = soup.find("a", href=re.compile(r"/[a-z]{2}/v/\d+-"))
        if not sale_anchor:
            return None
        value = sale_anchor.get_text(" ", strip=True)
        return value or None
