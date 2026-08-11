from __future__ import annotations

from typing import Optional

from ..db_interface import Database
from ..utils.auction_helpers import MAX_PHONE_LEN, clean_whitespace, fit_varchar, json_dumps
from .models import DorotheumLot


def build_auction_details(lot: DorotheumLot) -> dict[str, object]:
    return {
        "lotUid": lot.lot_uid,
        "lotNumber": lot.lot_id,
        "lotCategory": lot.lot_category,
        "auctionName": lot.auction_name,
        "auctionType": lot.auction_type,
        "auctionLocation": lot.auction_location,
        "auctionDateText": lot.auction_date_text,
        "startPrice": lot.start_price,
        "estimateLow": lot.estimate_low,
        "estimateHigh": lot.estimate_high,
        "currency": lot.currency,
        "expertName": lot.expert_name,
        "expertPhone": lot.expert_phone,
        "expertEmail": lot.expert_email,
    }


def resolve_artist_id(db: Database, artist_name: Optional[str]):
    if not artist_name:
        return None

    artist = db.get_or_create_artist(
        complete_name=artist_name,
        raw_data=json_dumps({"source": "dorotheum", "name": artist_name}),
    )
    return artist.artist_id


def resolve_default_auctioneer_id(db: Database):
    auctioneer = db.get_or_create_auctioneer(name="Dorotheum")
    auctioneer.raw_data = json_dumps(
        {
            "source": "dorotheum",
            "website": "https://www.dorotheum.com",
        }
    )
    db.flush()
    return auctioneer.auctioneer_id


def resolve_expert_id(db: Database, lot: DorotheumLot):
    full_name = clean_whitespace(lot.expert_name)
    if not full_name:
        return None

    first_name, last_name = _split_person_name(full_name)
    expert = db.get_or_create_expert(first_name=first_name, last_name=last_name)

    if lot.expert_phone:
        expert.phone = fit_varchar(lot.expert_phone, max_len=MAX_PHONE_LEN)
    if lot.expert_email:
        expert.email = fit_varchar(lot.expert_email)

    expert.raw_data = json_dumps(
        {
            "source": "dorotheum",
            "name": full_name,
            "phone": lot.expert_phone,
            "email": lot.expert_email,
        }
    )
    db.flush()
    return expert.expert_id


def _split_person_name(name: str) -> tuple[str, str]:
    parts = name.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]
