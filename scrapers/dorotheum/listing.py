from __future__ import annotations

import json
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .constants import LOT_PATH_RE, LOT_UID_RE, LOTS_SCRIPT_RE, PARSER, ROOT_URL
from .models import DorotheumListingLot
from .normalization import DorotheumNormalizationMixin


class DorotheumListingMixin(DorotheumNormalizationMixin):
    def extract_listing_lots(self, html: str) -> list[DorotheumListingLot]:
        payload = self._extract_lots_payload(html)
        if payload is None:
            return []

        lots_raw, branches_raw = payload
        branch_lookup = self._build_branch_lookup(branches_raw)

        listing_lots: list[DorotheumListingLot] = []
        for value in lots_raw.values():
            lot = self._listing_lot_from_payload(value, branch_lookup)
            if lot is not None:
                listing_lots.append(lot)
        return listing_lots

    def extract_lot_urls(self, html: str) -> list[str]:
        listing_lots = self.extract_listing_lots(html)
        if listing_lots:
            return [lot.lot_url for lot in listing_lots]

        soup = BeautifulSoup(html, PARSER)
        out: list[str] = []
        seen: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", "")).strip()
            if not LOT_PATH_RE.match(href):
                continue

            lot_url = urljoin(ROOT_URL, href)
            if lot_url in seen:
                continue

            seen.add(lot_url)
            out.append(lot_url)

        return out

    @staticmethod
    def _extract_lots_payload(html: str) -> Optional[tuple[dict[str, object], list[dict[str, object]]]]:
        match = LOTS_SCRIPT_RE.search(html)
        if not match:
            return None

        try:
            lots_raw = json.loads(match.group(1))
            branches_raw = json.loads(match.group(2))
        except json.JSONDecodeError:
            return None

        if not isinstance(lots_raw, dict) or not isinstance(branches_raw, list):
            return None
        return lots_raw, branches_raw

    def _build_branch_lookup(self, branches_raw: list[dict[str, object]]) -> dict[int, Optional[str]]:
        out: dict[int, Optional[str]] = {}
        for item in branches_raw:
            if not isinstance(item, dict):
                continue

            uid_raw = item.get("uid")
            if not str(uid_raw).isdigit():
                continue

            out[int(uid_raw)] = self._clean_text(item.get("name"))
        return out

    def _listing_lot_from_payload(
        self,
        value: object,
        branch_lookup: dict[int, Optional[str]],
    ) -> Optional[DorotheumListingLot]:
        if not isinstance(value, dict):
            return None

        uid = str(value.get("uid") or "").strip()
        if not uid:
            return None

        detail_url = self._clean_text(value.get("detailURL")) or f"/de/l/{uid}/"
        lot_url = urljoin(ROOT_URL, detail_url)
        if not LOT_UID_RE.search(lot_url):
            return None

        branch_name = None
        branch_id = value.get("filiale")
        if isinstance(branch_id, int):
            branch_name = branch_lookup.get(branch_id)

        artist_name = self._clean_artist_name(value.get("kuenstlername"))
        title = self._normalize_title(value.get("titel"), artist_name=artist_name)

        return DorotheumListingLot(
            uid=uid,
            lot_id=self._clean_text(value.get("publicNummer")),
            lot_url=lot_url,
            title=title,
            artist_name=artist_name,
            description=self._clean_text(value.get("beschreibung")) or "",
            auction_name=self._clean_text(value.get("auktion")),
            auction_type=self._clean_text(value.get("auctionTypeIdentifier")),
            auction_location=branch_name,
            auction_date=self._epoch_to_date(value.get("datum")),
            auction_end_timestamp=self._coerce_int(value.get("ablaufzeit")),
            currency=self._clean_text(value.get("currency")),
            start_price=self._parse_amount(value.get("preisFloat") or value.get("preis1")),
            lot_category=self._clean_text(value.get("warengruppeTitel")),
            image_urls=self._normalize_urls(value.get("images400x400")),
            raw_data=value,
        )
