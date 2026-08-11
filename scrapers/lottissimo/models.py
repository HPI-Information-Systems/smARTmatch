from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class LottissimoLot:
    lot_id: str
    lot_number: Optional[str]
    title: Optional[str]
    artist_name: Optional[str]
    description: str
    provenance: Optional[str]
    auction_details: str
    auction_date: Optional[date]
    auction_city: Optional[str]
    auction_country: Optional[str]
    image_urls: list[str]
    auctioneer_url: Optional[str]
    auctioneer_name: Optional[str]
    raw_data: str
