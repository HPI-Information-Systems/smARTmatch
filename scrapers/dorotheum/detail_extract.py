from __future__ import annotations

from datetime import date
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .constants import ESTIMATE_RANGE_RE, LOT_NUMBER_RE, LOT_UID_RE, ROOT_URL
from .normalization import DorotheumNormalizationMixin


class DorotheumDetailExtractMixin(DorotheumNormalizationMixin):
    @staticmethod
    def _extract_lot_uid(lot_url: str) -> Optional[str]:
        match = LOT_UID_RE.search(lot_url)
        if not match:
            return None
        return match.group(1)

    def _extract_lot_number(self, soup: BeautifulSoup) -> Optional[str]:
        for node in soup.select("p.headline"):
            text = self._clean_text(node.get_text(" ", strip=True))
            if not text:
                continue

            match = LOT_NUMBER_RE.search(text)
            if match:
                return self._clean_text(match.group(1))
        return None

    def _extract_title(self, soup: BeautifulSoup) -> Optional[str]:
        title_node = soup.select_one("span.lot-title-tooltip")
        if isinstance(title_node, Tag):
            title = self._clean_text(title_node.get_text(" ", strip=True))
            if title:
                return title

        headline = soup.select_one("h1.headline")
        if isinstance(headline, Tag):
            return self._clean_text(headline.get_text(" ", strip=True))
        return None

    def _extract_description(self, soup: BeautifulSoup) -> str:
        share_form = soup.select_one("#email-share-form")
        if isinstance(share_form, Tag):
            description = self._clean_text(share_form.get("data-beschreibung"))
            if description:
                return description

        for node in soup.select("div.bodytext-html p"):
            text = self._clean_text(node.get_text(" ", strip=True))
            if text:
                return text

        return ""

    def _extract_image_urls(self, soup: BeautifulSoup) -> list[str]:
        gallery = soup.select_one("div.lot-gallery-container")
        if not isinstance(gallery, Tag):
            return []

        payload = self._load_json(gallery.get("data-json"))
        if not isinstance(payload, list):
            return []

        out: list[str] = []
        seen: set[str] = set()
        for item in payload:
            if not isinstance(item, dict):
                continue

            candidate = self._pick_text(item.get("hires"), item.get("large"), item.get("bild"))
            if not candidate:
                continue

            full_url = urljoin(ROOT_URL, candidate)
            if full_url in seen:
                continue

            seen.add(full_url)
            out.append(full_url)

        return out

    def _extract_auction_date(self, soup: BeautifulSoup) -> tuple[Optional[date], Optional[str]]:
        time_node = soup.select_one("#auktion-details time[datetime]")
        if not isinstance(time_node, Tag):
            time_node = soup.select_one("table.auction-details-table time[datetime]")
        if not isinstance(time_node, Tag):
            return None, None

        datetime_raw = self._clean_text(time_node.get("datetime"))
        display_text = self._clean_text(time_node.get_text(" ", strip=True))
        return self._parse_datetime_to_date(datetime_raw), display_text

    def _extract_auction_table(self, soup: BeautifulSoup) -> dict[str, str]:
        table = soup.select_one("#auction-details-expanded")
        if not isinstance(table, Tag):
            return {}

        out: dict[str, str] = {}
        for row in table.select("tr"):
            key_node = row.find("th")
            value_node = row.find("td")
            if not isinstance(key_node, Tag) or not isinstance(value_node, Tag):
                continue

            key = self._clean_text(key_node.get_text(" ", strip=True))
            value = self._clean_text(value_node.get_text(" ", strip=True))
            if key and value:
                out[key.rstrip(":")] = value

        return out

    def _extract_amount_by_label(self, soup: BeautifulSoup, label: str) -> Optional[float]:
        return self._parse_amount(self._extract_value_by_label(soup, label))

    def _extract_value_by_label(self, soup: BeautifulSoup, label: str) -> Optional[str]:
        for dt in soup.select("#auktion-details dt"):
            dt_text = self._clean_text(dt.get_text(" ", strip=True))
            if not dt_text:
                continue
            if dt_text.rstrip(":").casefold() != label.casefold():
                continue

            dd = dt.find_next_sibling("dd")
            if isinstance(dd, Tag):
                return self._clean_text(dd.get_text(" ", strip=True))
        return None

    def _parse_estimate_range(self, value: Optional[str]) -> tuple[Optional[float], Optional[float]]:
        text = self._clean_text(value)
        if not text:
            return None, None

        numbers = [self._parse_amount(match.group(1)) for match in ESTIMATE_RANGE_RE.finditer(text)]
        values = [number for number in numbers if number is not None]
        if not values:
            return None, None
        if len(values) == 1:
            return values[0], None
        return values[0], values[1]
