from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from ..utils.auction_helpers import json_dumps
from .constants import LOT_ID_RE


class DrouotPageMetaMixin:
    def _extract_title(self, soup: BeautifulSoup, lot_url: str) -> str:
        h1 = soup.find("h1")
        if h1:
            text = self._normalize_heading_text(h1.get_text(" ", strip=True))
            if text:
                return text
        return f"Drouot lot {self._extract_lot_id(lot_url) or ''}".strip()

    @staticmethod
    def _normalize_heading_text(value: str) -> str:
        text = " ".join((value or "").split()).strip()
        if not text:
            return ""
        if len(text) > 220:
            text = text[:220].rstrip(" ,.-;:|")
        return text

    @staticmethod
    def _is_low_quality_title(value: Optional[str]) -> bool:
        text = " ".join((value or "").split()).strip()
        if not text:
            return True
        if len(text) < 4:
            return True
        if not re.search(r"[A-Za-zÀ-ÿ]", text):
            return True
        return False

    @staticmethod
    def _is_placeholder_title(value: Optional[str]) -> bool:
        text = " ".join((value or "").split()).strip().casefold()
        if not text:
            return True

        return any(
            phrase in text
            for phrase in (
                "nicht identifizierte signatur",
                "signature non identifiée",
                "signature non identifiee",
                "unidentified signature",
            )
        )

    def _extract_slug_title(self, *, lot_object: str, lot_url: str) -> Optional[str]:
        slug = self._extract_js_string(lot_object, "slug")
        if not slug:
            path_tail = urlparse(lot_url).path.rsplit("/", 1)[-1]
            slug = re.sub(r"^\d+-", "", path_tail)

        if not slug:
            return None

        text = unquote(slug).replace("/", " ").replace("-", " ")
        text = " ".join(text.split()).strip(" ,.-;:|")
        if not text or self._is_low_quality_title(text):
            return None

        if text.islower():
            text = text[0].upper() + text[1:]
        return text

    def _extract_product_schema(self, soup: BeautifulSoup) -> Optional[dict[str, object]]:
        for script in soup.find_all("script", type="application/ld+json"):
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

    def _build_raw_payload(
        self,
        *,
        lot_url: str,
        lot_object: str,
        product_schema: Optional[dict[str, object]],
        display_heading: Optional[str],
    ) -> str:
        payload = {
            "source": "drouot",
            "lot_url": lot_url,
            "display_heading": display_heading,
            "lot_object": lot_object or None,
            "structured_product": product_schema,
        }
        return json_dumps(payload)

    def _extract_lot_id(self, url: str) -> Optional[str]:
        match = LOT_ID_RE.search(url)
        if not match:
            return None
        return match.group(1)

    def _extract_live_date_text(self, soup: BeautifulSoup) -> Optional[str]:
        text = soup.get_text(" ", strip=True)
        match = re.search(r"LIVE\s+([^<]{5,40}?\|\s*[^<]{2,20})", text)
        if not match:
            return None
        return " ".join(match.group(1).split())

    def _extract_auction_date(self, lot_object: str) -> tuple[Optional[date], Optional[str]]:
        ts = self._extract_js_number(lot_object, "date")
        tz = self._extract_js_string(lot_object, "timeZone")
        if ts is None:
            return None, tz

        try:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.date(), tz
        except Exception:
            return None, tz

    def _extract_image_urls(self, lot_object: str) -> list[str]:
        photos_block = self._extract_js_array_block(lot_object, "photos")
        if not photos_block:
            return []

        paths = re.findall(r'path:"([^\"]+)"', photos_block)
        out: list[str] = []
        seen: set[str] = set()
        for path in paths:
            url = f"https://cdn.drouot.com/d/image/lot?size=fullHD&path={path}"
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out
