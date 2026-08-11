from __future__ import annotations

from typing import Optional

from ..db_interface import Database
from ..utils.auction_helpers import fit_varchar, json_dumps
from .models import SothebysLot


def build_auction_details(lot: SothebysLot) -> dict[str, object]:
    return {
        "lotNumber": lot.lot_number,
        "lotNumberType": lot.lot_number_type,
        "lotNumberVisible": lot.lot_number_visible,
        "estimateLow": lot.estimate_low,
        "estimateHigh": lot.estimate_high,
        "estimateType": lot.estimate_type,
        "estimateUponRequest": lot.estimate_upon_request,
        "auctionId": lot.auction_id,
        "auctionTitle": lot.auction_title,
        "auctionLocation": lot.auction_location,
        "auctionDepartments": lot.auction_departments,
        "auctionAcceptsBids": lot.auction_accepts_bids,
        "auctionGoesLive": lot.auction_goes_live,
        "auctionPublished": lot.auction_published,
        "auctionClosed": lot.auction_closed,
        "auctionYear": lot.auction_year,
        "auctionSlug": lot.auction_slug_name,
        "lotUrl": lot.lot_url,
    }


def resolve_artist_id(db: Database, artist_name: Optional[str]):
    cleaned_name = fit_varchar(artist_name)
    if not cleaned_name:
        return None

    artist = db.get_or_create_artist(
        complete_name=cleaned_name,
        raw_data=json_dumps({"source": "sothebys", "name": cleaned_name}),
    )
    return artist.artist_id


def resolve_default_auctioneer_id(db: Database):
    auctioneer = db.get_or_create_auctioneer(name="Sotheby's")
    auctioneer.raw_data = json_dumps(
        {
            "source": "sothebys",
            "website": "https://www.sothebys.com",
        }
    )
    db.flush()
    return auctioneer.auctioneer_id
