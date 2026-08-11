from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup

from .constants import AUCTIONEER_URL_RE, PARSER


class LottissimoAuctioneerMixin:
    def _extract_auctioneer_url(self, lot_url: str) -> Optional[str]:
        match = AUCTIONEER_URL_RE.search(lot_url)
        if not match:
            return None
        return match.group(1)

    def parse_auctioneer_page(self, html: str) -> tuple[str, str, str, str]:
        try:
            soup = BeautifulSoup(html, PARSER)
            summary = soup.find(attrs={"class": "auction-summary auctioneers-landing-page"})
            root = summary if summary is not None else soup

            name_tag = root.find(attrs={"itemprop": "name"})
            name = name_tag.get_text(strip=True) if name_tag else ""

            def t(itemprop: str) -> str:
                tag = root.find(attrs={"itemprop": itemprop})
                return tag.get_text(" ", strip=True) if tag else ""

            street = t("streetAddress")
            town = t("addressLocality")
            region = t("addressRegion")
            postal = t("postalCode")
            country = t("addressCountry")
            address_parts = [p for p in [street, town, region, postal, country] if p]
            address = ", ".join(address_parts)

            phone_tag = root.find(attrs={"class": "phone details"})
            phone = phone_tag.get_text(" ", strip=True) if phone_tag else ""

            email = ""
            match = re.search(r"mailto:([^\s\"'>]+)", html)
            if match:
                email = match.group(1)

            return name, address, phone, email
        except Exception as error:
            self._log(f"[fail] parse auctioneer page: {error}")
            return "", "", "", ""
