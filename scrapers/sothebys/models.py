from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class AuctionContext:
    auction_url: str
    auction_id: str


@dataclass
class SothebysLot:
    lot_id: str
    lot_number: Optional[str]
    lot_number_type: Optional[str]
    lot_number_visible: Optional[bool]
    lot_url: Optional[str]
    title: str
    artist_name: Optional[str]
    description: Optional[str]
    provenance: Optional[str]
    literature: Optional[str]
    image_urls: list[str]
    estimate_low: Optional[float]
    estimate_high: Optional[float]
    estimate_type: Optional[str]
    estimate_upon_request: Optional[bool]
    auction_id: Optional[str]
    auction_title: Optional[str]
    auction_location: Optional[str]
    auction_departments: Optional[str]
    auction_accepts_bids: Optional[str]
    auction_goes_live: Optional[str]
    auction_published: Optional[str]
    auction_closed: Optional[str]
    auction_year: Optional[str]
    auction_slug_name: Optional[str]
    raw_data: str
    auction_date: Optional[date]
