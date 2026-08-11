from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class DrouotLot:
    lot_id: str
    lot_number: Optional[str]
    title: Optional[str]
    artist_name: Optional[str]
    description: str
    image_urls: list[str]
    start_price: Optional[float]
    estimate_low: Optional[float]
    estimate_high: Optional[float]
    currency: Optional[str]
    buyer_fees_percent: Optional[float]
    auction_date: Optional[date]
    auction_date_text: Optional[str]
    auction_timezone: Optional[str]
    auction_location: Optional[str]
    auctioneer_name: Optional[str]
    auctioneer_url: Optional[str]
    auctioneer_phone: Optional[str]
    auctioneer_email: Optional[str]
    auctioneer_address: Optional[str]
    catalogue_name: Optional[str]
    lot_category: Optional[str]
    lot_primary_category: Optional[str]
    auction_city: Optional[str]
    auction_country: Optional[str]
    lot_start_date: Optional[str]
    lot_end_date: Optional[str]
    bidding_type: Optional[str]
    auction_accepts_bids: Optional[bool]
    auction_goes_live: Optional[bool]
    auction_published: Optional[bool]
    auction_closed: Optional[bool]
    raw_data: str
