from __future__ import annotations

import json
import re
from typing import Optional

from bs4 import BeautifulSoup

from ..utils.request_handler import request_html
from .parser import normalize_text


class ChristiesLotParser:
    def merge_data(
        self,
        api_data: Optional[dict[str, object]],
        html_data: Optional[dict[str, object]],
    ) -> tuple[dict[str, object], dict[str, object], Optional[dict[str, object]]]:
        lot_data: dict[str, object] = {}
        sale_data: dict[str, object] = {}
        specialist: Optional[dict[str, object]] = None

        if api_data and isinstance(api_data, dict):
            lot_data.update(api_data)
            sale_data.update(api_data.get("sale", {}) if isinstance(api_data.get("sale"), dict) else {})

        if html_data and isinstance(html_data, dict):
            html_lot = html_data.get("lots") if isinstance(html_data.get("lots"), dict) else None
            if html_lot:
                for key, value in html_lot.items():
                    if value is not None and value != "":
                        lot_data[key] = value

            html_sale = html_data.get("sale") if isinstance(html_data.get("sale"), dict) else None
            if html_sale:
                for key, value in html_sale.items():
                    if value is not None and value != "":
                        sale_data[key] = value

            specialist = html_data.get("chr-specialist") if isinstance(html_data.get("chr-specialist"), dict) else None

        return lot_data, sale_data, specialist

    @staticmethod
    def extract_lot_id(lot: dict[str, object]) -> Optional[str]:
        for key in ("object_id", "analytics_id", "objectIdDotCom", "id"):
            value = lot.get(key)
            if value:
                return str(value)
        return None

    def extract_title_artist_from_webpage(self, lot_url: str) -> tuple[Optional[str], Optional[str]]:
        html = request_html(lot_url, min_wait=0.0, max_wait=0.0)
        if not html:
            return None, None

        try:
            soup = BeautifulSoup(html, "lxml")
            return self.extract_title_artist_from_soup(soup)
        except Exception:
            return None, None

    def extract_title_artist_from_soup(self, soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
        title_tag = soup.select_one("h1.chr-lot-header__title") or soup.select_one("h1.chr-lot-header__title-text")
        artist_tag = soup.select_one("span.chr-lot-header__artist-name")

        title = title_tag.get_text(" ", strip=True) if title_tag else None
        artist = artist_tag.get_text(" ", strip=True) if artist_tag else None

        for selector, attr in (("title", None), ('meta[name="title"]', "content")):
            if title and artist:
                break

            node = soup.select_one(selector)
            if not node:
                continue

            value = node.get_text(" ", strip=True) if attr is None else str(node.get(attr) or "").strip()
            if not value:
                continue

            parsed_artist, parsed_title = self.parse_combined_page_title(value)
            if not title and parsed_title:
                title = parsed_title
            if not artist and parsed_artist:
                artist = parsed_artist

        json_ld = self.extract_product_json_ld(soup)
        if json_ld:
            if not artist:
                artist = normalize_text(json_ld.get("name"))
            if not title:
                title = normalize_text(json_ld.get("brand"))

        if not artist:
            og_title = soup.select_one('meta[property="og:title"]')
            if og_title and og_title.get("content"):
                artist = normalize_text(str(og_title.get("content")))

        title = normalize_text(title)
        artist = normalize_text(artist)
        return title, artist

    @staticmethod
    def extract_product_json_ld(soup: BeautifulSoup) -> Optional[dict[str, object]]:
        for script in soup.select('script[type="application/ld+json"]'):
            payload = (script.get_text() or "").strip()
            if not payload:
                continue

            try:
                data = json.loads(payload)
            except Exception:
                continue

            candidates = data if isinstance(data, list) else [data]
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("@type") == "Product":
                    return candidate

        return None

    def parse_combined_page_title(self, value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        if not value or not isinstance(value, str):
            return None, None

        text = value.strip()
        if not text:
            return None, None

        text = re.sub(r"\|\s*Christie(?:'|’)s.*$", "", text, flags=re.IGNORECASE).strip()

        if "," in text:
            artist, title = text.split(",", 1)
            artist = normalize_text(artist)
            title = normalize_text(title)
            return artist, title

        return None, normalize_text(text)

    @staticmethod
    def lot_url_for_id(lot_id: str) -> str:
        if "." in lot_id:
            parts = lot_id.split(".")
            lot_num = ".".join(parts[1:])
            return f"https://onlineonly.christies.com/sso?objectid={lot_id}&lotnumber={lot_num}"
        return f"https://www.christies.com/en/lot/lot-{lot_id}"
