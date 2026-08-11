from __future__ import annotations

from typing import Optional

from .lot_extract import (
    build_lot_url,
    build_raw_payload,
    clean_text,
    extract_auction_block,
    extract_auction_date,
    extract_auction_dates,
    extract_auction_slug_name,
    extract_auction_year,
    extract_departments,
    extract_estimate_info,
    extract_image_urls,
    extract_lot_block,
    extract_lot_id,
    extract_lot_number_info,
    pick_best_rendition_url,
    parse_amount,
)
from .models import SothebysLot


class SothebysLotParser:
    _pick_best_rendition_url = staticmethod(pick_best_rendition_url)
    _parse_amount = staticmethod(parse_amount)

    def parse_lot_response(self, response: dict) -> Optional[SothebysLot]:
        lot = extract_lot_block(response)
        if not lot or lot.get("__typename") == "HiddenLot":
            return None

        lot_id = extract_lot_id(lot)
        if not lot_id:
            return None

        auction = extract_auction_block(lot)
        auction_dates = extract_auction_dates(auction)
        lot_number, lot_number_type, lot_number_visible = extract_lot_number_info(lot)
        estimate_low, estimate_high, estimate_type, estimate_upon_request = extract_estimate_info(lot)

        return SothebysLot(
            lot_id=lot_id,
            lot_number=lot_number,
            lot_number_type=lot_number_type,
            lot_number_visible=lot_number_visible,
            lot_url=build_lot_url(lot=lot, auction=auction),
            title=self._extract_title(lot, lot_id=lot_id),
            artist_name=clean_text(lot.get("creatorsDisplayTitle")),
            description=self._build_description(lot),
            provenance=clean_text(lot.get("provenance")),
            literature=clean_text(lot.get("literature")),
            image_urls=extract_image_urls(lot),
            estimate_low=estimate_low,
            estimate_high=estimate_high,
            estimate_type=estimate_type,
            estimate_upon_request=estimate_upon_request,
            auction_id=clean_text(auction.get("auctionId")),
            auction_title=clean_text(auction.get("title")),
            auction_location=clean_text(auction.get("location")),
            auction_departments=extract_departments(auction),
            auction_accepts_bids=auction_dates.get("acceptsBids"),
            auction_goes_live=auction_dates.get("goesLive"),
            auction_published=auction_dates.get("published"),
            auction_closed=auction_dates.get("closed"),
            auction_year=extract_auction_year(auction),
            auction_slug_name=extract_auction_slug_name(auction),
            raw_data=build_raw_payload(response),
            auction_date=extract_auction_date(auction_dates),
        )

    @staticmethod
    def _extract_title(lot: dict, *, lot_id: str) -> str:
        return clean_text(lot.get("title")) or f"Lot {lot_id}"

    @staticmethod
    def _build_description(lot: dict) -> Optional[str]:
        parts: list[str] = []
        for key in ("description", "catalogueNote", "exhibition"):
            value = clean_text(lot.get(key))
            if value:
                parts.append(value)

        if not parts:
            return None
        return "\n\n".join(parts)
