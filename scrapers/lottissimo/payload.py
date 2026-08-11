from __future__ import annotations

from datetime import date
from typing import Optional

from bs4 import BeautifulSoup

from ..utils.auction_helpers import json_dumps


class LottissimoPayloadMixin:
    def _extract_meta_content(
        self,
        soup: BeautifulSoup,
        *,
        name: Optional[str] = None,
        property_name: Optional[str] = None,
    ) -> Optional[str]:
        if name:
            tag = soup.find("meta", attrs={"name": name})
        else:
            tag = soup.find("meta", attrs={"property": property_name})
        if not tag:
            return None
        content = tag.get("content")
        if not isinstance(content, str):
            return None
        cleaned = content.strip()
        return cleaned or None

    def _build_raw_payload(
        self,
        *,
        soup: BeautifulSoup,
        lot_url: str,
        raw_title: str,
        data_layer_payload: Optional[str],
        data_layer: Optional[dict[str, str]],
        description_tag,
        auction_details_tag,
    ) -> str:
        payload = {
            "source": "lot-tissimo",
            "lot_url": lot_url,
            "raw_title": raw_title,
            "page_title": soup.title.get_text(" ", strip=True) if soup.title else None,
            "og_title": self._extract_meta_content(soup, property_name="og:title"),
            "og_description": self._extract_meta_content(soup, property_name="og:description"),
            "data_layer_raw": data_layer_payload,
            "data_layer": data_layer,
            "description_html": str(description_tag) if description_tag else None,
            "auction_html": str(auction_details_tag) if auction_details_tag else None,
        }
        return json_dumps(payload)

    def _auction_details_from_meta(
        self,
        *,
        meta: Optional[dict[str, str]],
        fallback: str,
        auction_city: Optional[str],
        auction_country: Optional[str],
        lot_number: Optional[str],
        auction_date: Optional[date],
        auctioneer_url: Optional[str],
    ) -> str:
        payload: dict[str, object] = dict(meta or {})

        if lot_number:
            payload["lotNumber"] = lot_number

        if auction_date:
            payload["auctionDate"] = auction_date.isoformat()

        payload["auctionCity"] = auction_city or payload.get("auctionCity")
        payload["auctionCountry"] = auction_country or payload.get("auctionCountry")
        payload["auctioneerUrl"] = auctioneer_url
        payload["auctionTabText"] = (fallback or "").strip() or None

        if meta:
            estimate_low = self._parse_decimal(meta.get("minEstimate"))
            estimate_high = self._parse_decimal(meta.get("maxEstimate"))
            start_price = self._parse_decimal(meta.get("openingPrice"))

            payload.pop("minEstimate", None)
            if estimate_low and estimate_low != 0:
                payload["estimateLow"] = estimate_low

            payload.pop("maxEstimate", None)
            if estimate_high and estimate_high != 0:
                payload["estimateHigh"] = estimate_high

            payload.pop("openingPrice", None)
            if start_price and start_price != 0:
                payload["startPrice"] = start_price

            current_watchers = self._parse_int(meta.get("currentWatchers"))
            if current_watchers and current_watchers != 0:
                payload["currentWatchers"] = current_watchers
            else:
                payload.pop("currentWatchers", None)

            current_bids = self._parse_int(meta.get("currentBids"))
            if current_bids and current_bids != 0:
                payload["currentBids"] = current_bids
            else:
                payload.pop("currentBids", None)

            lot_image_count = self._parse_int(meta.get("lotImageCount"))
            if lot_image_count and lot_image_count != 0:
                payload["lotImageCount"] = lot_image_count
            else:
                payload.pop("lotImageCount", None)

            payload["deliveryAvailable"] = self._parse_bool(meta.get("deliveryAvailable"))
            payload["featuredLot"] = self._parse_bool(meta.get("featuredLot"))
            payload["hasLotDescription"] = self._parse_bool(meta.get("hasLotDescription"))

        if not payload and fallback:
            payload["auctionTabText"] = fallback

        return json_dumps(payload)
