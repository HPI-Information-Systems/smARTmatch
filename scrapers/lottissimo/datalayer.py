from __future__ import annotations

import json
import re
from datetime import date
from typing import Optional

from bs4 import BeautifulSoup

from .constants import (
    AUCTION_CITY_RE,
    AUCTION_COUNTRY_RE,
    DATA_LAYER_PUSH_MARKER,
    LOT_END_DATE_RE,
    LOT_NUMBER_JSON_RE,
    LOT_START_DATE_RE,
)
from .value_parsing import (
    normalize_lot_number,
    parse_bool,
    parse_decimal,
    parse_int,
    parse_iso_date,
)


class LottissimoDataLayerMixin:
    def _extract_display_lot_number(
        self,
        html: str,
        soup: BeautifulSoup,
        meta: Optional[dict[str, str]] = None,
    ) -> Optional[str]:
        for selector in (".lot-details p.lot-number", ".lot-details .lot-number", "p.lot-number"):
            node = soup.select_one(selector)
            if not node:
                continue
            candidate = self._normalize_lot_number(node.get_text(" ", strip=True))
            if candidate:
                return candidate

        for attr in ("data-lot-number", "data-lotno", "data-lot"):
            tag = soup.find(attrs={attr: True})
            if not tag:
                continue
            candidate = self._normalize_lot_number(str(tag.get(attr)))
            if candidate:
                return candidate

        match = LOT_NUMBER_JSON_RE.search(html)
        if match:
            candidate = self._normalize_lot_number(match.group(1))
            if candidate:
                return candidate

        if meta:
            candidate = self._normalize_lot_number(meta.get("lotDescription"))
            if candidate:
                return candidate

        return None

    def _extract_display_lot_id(self, html: str, soup: BeautifulSoup, fallback: str) -> str:
        return self._extract_display_lot_number(html, soup) or fallback

    @staticmethod
    def _normalize_lot_number(value: Optional[str]) -> Optional[str]:
        return normalize_lot_number(value)

    @staticmethod
    def _resolve_lot_id(fallback: str, meta: Optional[dict[str, str]]) -> str:
        if meta:
            candidate = (meta.get("lotId") or "").strip()
            if candidate:
                return candidate
        return fallback

    def _extract_auction_metadata(
        self,
        html: str,
        meta: Optional[dict[str, str]] = None,
    ) -> tuple[Optional[date], Optional[str], Optional[str]]:
        if meta:
            parsed_date = self._parse_iso_date(meta.get("lotStartDate")) or self._parse_iso_date(
                meta.get("lotEndDate")
            )
            city = (meta.get("auctionCity") or "").strip() or None
            country = (meta.get("auctionCountry") or "").strip() or None
            if parsed_date or city or country:
                return parsed_date, city, country

        start_date = None
        match = LOT_START_DATE_RE.search(html)
        if match:
            start_date = match.group(1).strip()

        end_date = None
        match = LOT_END_DATE_RE.search(html)
        if match:
            end_date = match.group(1).strip()

        city = None
        match = AUCTION_CITY_RE.search(html)
        if match:
            city = match.group(1).strip() or None

        country = None
        match = AUCTION_COUNTRY_RE.search(html)
        if match:
            country = match.group(1).strip() or None

        return self._parse_iso_date(start_date) or self._parse_iso_date(end_date), city, country

    def _extract_datalayer_dict(self, html: str) -> Optional[dict[str, str]]:
        payload = self._extract_datalayer_push_object(html)
        if not payload:
            return None

        try:
            data = json.loads(payload)
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        out: dict[str, str] = {}
        for key, value in data.items():
            if isinstance(value, (str, int, float, bool)):
                out[str(key)] = str(value)
        return out

    def _extract_datalayer_push_object(self, html: str) -> Optional[str]:
        marker_idx = html.find(DATA_LAYER_PUSH_MARKER)
        if marker_idx == -1:
            return None

        start = html.find("{", marker_idx)
        if start == -1:
            return None

        depth = 0
        in_string = False
        escape = False

        for i in range(start, len(html)):
            ch = html[i]

            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
                continue

            if ch == "{":
                depth += 1
                continue

            if ch == "}":
                depth -= 1
                if depth == 0:
                    payload = html[start : i + 1]
                    payload = re.sub(r",\s*\}", "}", payload)
                    return payload

        return None

    @staticmethod
    def _parse_iso_date(value: Optional[str]) -> Optional[date]:
        return parse_iso_date(value)

    @staticmethod
    def _parse_decimal(value: Optional[str]) -> Optional[float]:
        return parse_decimal(value)

    @staticmethod
    def _parse_int(value: Optional[str]) -> Optional[int]:
        return parse_int(value)

    @staticmethod
    def _parse_bool(value: Optional[str]) -> Optional[bool]:
        return parse_bool(value)
