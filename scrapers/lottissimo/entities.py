from __future__ import annotations

from typing import Callable, Optional

from ..db_interface import Auctioneer, Database
from ..utils.auction_helpers import MAX_PHONE_LEN, fit_varchar, json_dumps
from .parser import LottissimoLotParser


def resolve_artist_id(db: Database, artist_name: Optional[str]):
    cleaned_name = fit_varchar(artist_name)
    if not cleaned_name:
        return None

    artist = db.get_or_create_artist(
        complete_name=cleaned_name,
        raw_data=json_dumps({"source": "lot-tissimo", "name": cleaned_name}),
    )
    return artist.artist_id


def resolve_auctioneer(
    *,
    db: Database,
    parser: LottissimoLotParser,
    fetch_html: Callable[[str], str],
    cache: dict[str, Optional[Auctioneer]],
    auctioneer_url: Optional[str],
    fallback_name: Optional[str],
) -> Optional[Auctioneer]:
    cache_key = auctioneer_url or f"name:{(fallback_name or '').strip().casefold()}"
    if cache_key in cache:
        return cache[cache_key]

    profile_name = ""
    profile_address = ""
    profile_phone = ""
    profile_email = ""

    if auctioneer_url:
        html = fetch_html(auctioneer_url)
        if html:
            profile_name, profile_address, profile_phone, profile_email = parser.parse_auctioneer_page(html)

    name = fit_varchar(profile_name or fallback_name)
    if not name:
        cache[cache_key] = None
        return None

    auctioneer = db.get_or_create_auctioneer(name=name)
    if profile_address:
        auctioneer.address = fit_varchar(profile_address)
    if profile_phone:
        auctioneer.phone = fit_varchar(profile_phone, max_len=MAX_PHONE_LEN)
    if profile_email:
        auctioneer.email = fit_varchar(profile_email)

    auctioneer.raw_data = json_dumps(
        {
            "source": "lot-tissimo",
            "auctioneer_url": auctioneer_url,
            "fallback_name": fit_varchar(fallback_name),
        }
    )
    db.flush()

    parser._log(f"[save] auctioneer '{name}'")
    cache[cache_key] = auctioneer
    return auctioneer
