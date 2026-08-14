from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .artist import LottissimoArtistMixin
from .auctioneer import LottissimoAuctioneerMixin
from .constants import IMAGE_RE, LOT_ID_RE, LOT_LINK_RE, PARSER, ROOT_URL
from .datalayer import LottissimoDataLayerMixin
from .models import LottissimoLot
from .payload import LottissimoPayloadMixin


class LottissimoLotParser(
    LottissimoArtistMixin,
    LottissimoDataLayerMixin,
    LottissimoPayloadMixin,
    LottissimoAuctioneerMixin,
):
    def __init__(self, *, log=print) -> None:
        # ``log`` is the bound ``Scraper.log`` of the owning scraper.
        self._log = log

    def extract_lot_urls(self, html: str) -> list[str]:
        soup = BeautifulSoup(html, PARSER)
        out: list[str] = []
        seen: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", "")).strip()
            match = LOT_LINK_RE.search(href)
            if not match:
                continue

            lot_url = urljoin(ROOT_URL, match.group(0))
            if lot_url in seen:
                continue
            seen.add(lot_url)
            out.append(lot_url)

        return out

    def extract_lot_id(self, url: str, lot_id_pattern: re.Pattern[str]) -> str:
        matches = lot_id_pattern.findall(url)
        if matches:
            return matches[-1]
        return url.rstrip("/").split("/")[-1]

    def parse_lot_page(self, *, html: str, lot_url: str) -> Optional[LottissimoLot]:
        try:
            soup = BeautifulSoup(html, PARSER)
            lot_id_hint = self.extract_lot_id(lot_url, LOT_ID_RE)
            auction_meta_payload = self._extract_datalayer_push_object(html)
            auction_meta_dict = self._extract_datalayer_dict(html)

            resolved_lot_id = self._resolve_lot_id(lot_id_hint, auction_meta_dict)
            lot_number = self._extract_display_lot_number(html, soup, auction_meta_dict)
            auction_date, auction_city, auction_country = (
                self._extract_auction_metadata(html, auction_meta_dict)
            )

            raw_title = self._extract_raw_title(
                soup=soup, lot_id=resolved_lot_id, meta=auction_meta_dict
            )
            title, title_artist = self._split_title_and_artist(raw_title)
            artist_name = self._extract_artist_name(auction_meta_dict, title_artist)

            description_tag = self._find_tab_segment(soup, tab_name="description")
            auction_details_tag = self._find_tab_segment(soup, tab_name="auction")
            description, provenance = self._extract_description_and_provenance(
                description_tag
            )
            auctioneer_url = self._extract_auctioneer_url(lot_url)

            auction_details = self._auction_details_from_meta(
                meta=auction_meta_dict,
                fallback=self._extract_tag_text(auction_details_tag),
                auction_city=auction_city,
                auction_country=auction_country,
                lot_number=lot_number,
                auction_date=auction_date,
                auctioneer_url=auctioneer_url,
            )

            raw_data = self._build_raw_payload(
                soup=soup,
                lot_url=lot_url,
                raw_title=raw_title,
                data_layer_payload=auction_meta_payload,
                data_layer=auction_meta_dict,
                description_tag=description_tag,
                auction_details_tag=auction_details_tag,
            )

            return LottissimoLot(
                lot_id=resolved_lot_id,
                lot_number=lot_number,
                title=title,
                artist_name=artist_name,
                description=description,
                provenance=provenance,
                auction_details=auction_details,
                auction_date=auction_date,
                auction_city=auction_city,
                auction_country=auction_country,
                image_urls=self._extract_image_urls(soup),
                auctioneer_url=auctioneer_url,
                auctioneer_name=self._extract_auctioneer_name(auction_meta_dict),
                raw_data=raw_data,
            )
        except Exception as error:
            self._log(f"[fail] parse lot page {lot_url}: {error}")
            return None

    @staticmethod
    def _find_tab_segment(soup: BeautifulSoup, *, tab_name: str) -> Optional[Tag]:
        candidates = [
            node
            for node in soup.select(f'[data-tab="{tab_name}"]')
            if isinstance(node, Tag)
        ]
        if not candidates:
            return None

        return max(candidates, key=LottissimoLotParser._score_tab_candidate)

    @staticmethod
    def _score_tab_candidate(node: Tag) -> int:
        classes = {
            class_name.lower()
            for class_name in node.get("class", [])
            if isinstance(class_name, str)
        }
        score = 0

        if "segment" in classes:
            score += 100
        if "tab" in classes:
            score += 30
        if "content" in classes or "pane" in classes:
            score += 20
        if "item" in classes:
            score -= 50
        if "menu" in classes:
            score -= 75
        if node.name in {"a", "button", "li", "span"}:
            score -= 120

        if node.find(True):
            score += 20

        text = node.get_text(" ", strip=True)
        score += min(len(text), 500) // 10

        return score

    _PROVENANCE_HEADING_RE = re.compile(
        r"^\s*(?:provenienz|provenance|provenienza)\s*:?\s*$",
        flags=re.IGNORECASE,
    )
    _PROVENANCE_INLINE_RE = re.compile(
        r"^\s*(?:provenienz|provenance|provenienza)\s*:?\s*(.+?)\s*$",
        flags=re.IGNORECASE,
    )

    @staticmethod
    def _extract_tag_text(node: Optional[Tag]) -> str:
        if not node:
            return ""
        return node.get_text(separator="\n", strip=True)

    @classmethod
    def _extract_description_and_provenance(
        cls, node: Optional[Tag]
    ) -> tuple[str, Optional[str]]:
        text = cls._extract_tag_text(node)
        if not text:
            return "", None

        raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not raw_lines:
            return "", None

        description_lines: list[str] = []
        provenance_lines: list[str] = []
        skip_next = False

        for idx, line in enumerate(raw_lines):
            if skip_next:
                skip_next = False
                continue

            if cls._PROVENANCE_HEADING_RE.match(line):
                next_line = (
                    raw_lines[idx + 1].strip() if idx + 1 < len(raw_lines) else ""
                )
                if next_line:
                    provenance_lines.append(next_line)
                    skip_next = True
                continue

            inline_match = cls._PROVENANCE_INLINE_RE.match(line)
            if inline_match:
                inline_value = inline_match.group(1).strip()
                if inline_value:
                    provenance_lines.append(inline_value)
                continue

            description_lines.append(line)

        description = "\n".join(description_lines).strip()

        deduped_provenance: list[str] = []
        seen: set[str] = set()
        for line in provenance_lines:
            key = line.casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped_provenance.append(line)

        provenance = "\n".join(deduped_provenance).strip() or None
        return description, provenance

    def _extract_raw_title(
        self, *, soup: BeautifulSoup, lot_id: str, meta: Optional[dict[str, str]]
    ) -> str:
        title_tag = soup.select_one("h1.header-lot-title")
        if title_tag:
            title = title_tag.get_text(" ", strip=True)
            if title:
                return title

        if meta:
            title = (meta.get("lotName") or "").strip()
            if title:
                return title

        return ""

    @staticmethod
    def _extract_artist_name(
        meta: Optional[dict[str, str]], title_artist: Optional[str]
    ) -> Optional[str]:
        if meta:
            for key in ("artistName", "artist", "lotArtist"):
                value = (meta.get(key) or "").strip()
                if value:
                    return value
        return title_artist

    @staticmethod
    def _extract_auctioneer_name(meta: Optional[dict[str, str]]) -> Optional[str]:
        if not meta:
            return None
        value = (meta.get("auctioneer") or "").strip()
        return value or None

    def _extract_image_urls(self, soup: BeautifulSoup) -> list[str]:
        # The current site uses ``.lot-image``; older and expired pages may
        # still expose ``.touch-swipe-gallery``. Restrict extraction to these
        # containers so recommendation cards and auctioneer logos are ignored.
        galleries = soup.select(".touch-swipe-gallery, .lot-image")
        gallery_html = "".join(str(gallery) for gallery in galleries)

        out: list[str] = []
        seen: set[str] = set()
        for match in IMAGE_RE.findall(gallery_html):
            normalized = match.split("?", 1)[0]
            if normalized in seen:
                continue
            seen.add(normalized)
            out.append(normalized)
        return out
