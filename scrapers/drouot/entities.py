from __future__ import annotations

from typing import Optional

from ..db_interface import Database
from ..utils.auction_helpers import MAX_PHONE_LEN, fit_varchar, json_dumps
from .models import DrouotLot


def build_auction_details(lot: DrouotLot) -> dict[str, object]:
    return {
        "catalogueName": lot.catalogue_name,
        "lotNumber": lot.lot_number,
        "lotCategory": lot.lot_category,
        "lotPrimaryCategory": lot.lot_primary_category,
        "auctionCity": lot.auction_city,
        "auctionCountry": lot.auction_country,
        "lotStartDate": lot.lot_start_date,
        "lotEndDate": lot.lot_end_date,
        "biddingType": lot.bidding_type,
        "minEstimate": lot.estimate_low,
        "maxEstimate": lot.estimate_high,
        "estimateLow": lot.estimate_low,
        "estimateHigh": lot.estimate_high,
        "auctionAcceptsBids": lot.auction_accepts_bids,
        "auctionGoesLive": lot.auction_goes_live,
        "auctionPublished": lot.auction_published,
        "auctionClosed": lot.auction_closed,
        "startPrice": lot.start_price,
        "currency": lot.currency,
        "buyerPremiumPercent": lot.buyer_fees_percent,
        "auctioneerName": lot.auctioneer_name,
        "auctioneerPhone": lot.auctioneer_phone,
        "auctioneerEmail": lot.auctioneer_email,
        "auctioneerAddress": lot.auctioneer_address,
        "auctioneerUrl": lot.auctioneer_url,
        "auctionDateText": lot.auction_date_text,
        "auctionTimezone": lot.auction_timezone,
        "auctionLocation": lot.auction_location,
    }


def resolve_artist_id(db: Database, artist_name: Optional[str]):
    cleaned_name = fit_varchar(artist_name)
    if not cleaned_name:
        return None

    artist = db.get_or_create_artist(
        complete_name=cleaned_name,
        raw_data=json_dumps({"source": "drouot", "name": cleaned_name}),
    )
    return artist.artist_id


def resolve_auctioneer_id(db: Database, lot: DrouotLot):
    auctioneer_name = fit_varchar(lot.auctioneer_name)
    if not auctioneer_name:
        return None

    auctioneer = db.get_or_create_auctioneer(name=auctioneer_name)
    if lot.auctioneer_address:
        auctioneer.address = fit_varchar(lot.auctioneer_address)
    if lot.auctioneer_phone:
        auctioneer.phone = fit_varchar(lot.auctioneer_phone, max_len=MAX_PHONE_LEN)
    if lot.auctioneer_email:
        auctioneer.email = fit_varchar(lot.auctioneer_email)

    auctioneer.raw_data = json_dumps(
        {
            "source": "drouot",
            "auctioneer_url": lot.auctioneer_url,
        }
    )
    db.flush()
    return auctioneer.auctioneer_id
