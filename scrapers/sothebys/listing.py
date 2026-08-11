from __future__ import annotations

from typing import Iterable

from sqlalchemy import select

from ..db_interface import AuctionArtwork, Database


def iter_auction_urls(*, client, base_calendar_url: str, max_calendar_pages: int | None) -> Iterable[str]:
    seen: set[str] = set()
    prev_page_auction_links: list[str] | None = None

    page = 0
    while True:
        if max_calendar_pages is not None and page >= max_calendar_pages:
            break

        page_url = client.calendar_page_url(base_calendar_url, page)
        html = client.fetch_calendar_html(page_url)
        links = sorted(client.extract_buy_links(html, base_url=page_url))
        auction_links = [link for link in links if "/buy/auction/" in link]

        if not auction_links:
            if page == 0:
                page = 1
                continue
            break

        if prev_page_auction_links is not None and auction_links == prev_page_auction_links:
            break

        new_on_page = 0
        for link in auction_links:
            if link in seen:
                continue
            seen.add(link)
            new_on_page += 1
            yield link

        client._log(f"[calendar page {page}] found {len(auction_links)} auctions ({new_on_page} new)")
        prev_page_auction_links = auction_links
        page += 1

    client._log(f"[done] parsed {len(seen)} auctions across calendar pages")


def get_existing_lot_ids(*, db: Database, platform_id) -> set[str]:
    if not platform_id:
        return set()

    session = db._get_session()
    rows = session.execute(
        select(AuctionArtwork.lot_id).where(
            AuctionArtwork.auction_platform_id == platform_id,
            AuctionArtwork.lot_id.isnot(None),
        )
    ).all()

    return {lot_id for (lot_id,) in rows if isinstance(lot_id, str) and lot_id}
