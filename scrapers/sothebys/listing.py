from __future__ import annotations

from typing import Iterable

from sqlalchemy import select

from ..db_interface import AuctionArtwork, Database


def iter_auction_urls(
    *, client, base_calendar_url: str, max_calendar_pages: int | None
) -> Iterable[str]:
    seen: set[str] = set()

    # Sotheby's calendar is one-based: p=0 aliases p=1. Starting at zero made
    # the duplicate-page guard stop before the real second page (p=2).
    page_number = 1
    pages_fetched = 0
    while True:
        if max_calendar_pages is not None and pages_fetched >= max_calendar_pages:
            break

        page_url = client.calendar_page_url(base_calendar_url, page_number)
        pages_fetched += 1
        html = client.fetch_calendar_html(page_url)
        links = sorted(client.extract_buy_links(html, base_url=page_url))
        auction_links = [link for link in links if "/buy/auction/" in link]

        if not auction_links:
            break

        new_links = [link for link in auction_links if link not in seen]
        if not new_links:
            break

        for link in new_links:
            seen.add(link)
            yield link

        client._log(
            f"[calendar page {page_number}] found {len(auction_links)} auctions "
            f"({len(new_links)} new)"
        )
        page_number += 1

    client._log(
        f"[done] parsed {len(seen)} auctions across {pages_fetched} calendar pages"
    )


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
