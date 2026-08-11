from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class DorotheumListingLot:
    uid: str
    lot_id: Optional[str]
    lot_url: str
    title: Optional[str]
    artist_name: Optional[str]
    description: str
    auction_name: Optional[str]
    auction_type: Optional[str]
    auction_location: Optional[str]
    auction_date: Optional[date]
    auction_end_timestamp: Optional[int]
    currency: Optional[str]
    start_price: Optional[float]
    lot_category: Optional[str]
    image_urls: list[str]
    raw_data: dict[str, object]


@dataclass
class DorotheumLot:
    lot_uid: str
    lot_id: Optional[str]
    lot_url: str
    title: Optional[str]
    artist_name: Optional[str]
    description: str
    image_urls: list[str]
    auction_date: Optional[date]
    auction_date_text: Optional[str]
    auction_name: Optional[str]
    auction_type: Optional[str]
    auction_location: Optional[str]
    lot_category: Optional[str]
    start_price: Optional[float]
    estimate_low: Optional[float]
    estimate_high: Optional[float]
    currency: Optional[str]
    expert_name: Optional[str]
    expert_phone: Optional[str]
    expert_email: Optional[str]
    raw_data: str
