from __future__ import annotations

import unittest
from unittest.mock import patch

from scrapers.sothebys.models import AuctionContext
from scrapers.sothebys.scraper import SothebysScraper


class _FakeDb:
    session = None


class SothebysSkipMetricTests(unittest.TestCase):
    def test_get_urls_tracks_existing_lots_filtered_before_processing(self) -> None:
        scraper = SothebysScraper(db=_FakeDb())
        context = AuctionContext(
            auction_url="https://example.test/auction",
            auction_id="auction-1",
        )

        with (
            patch.object(scraper, "_get_auction_urls", return_value=[context.auction_url]),
            patch.object(scraper, "_get_existing_lot_ids", return_value={"lot-1", "lot-3"}),
            patch.object(scraper, "_get_auction_context", return_value=context),
            patch.object(
                scraper._client,
                "fetch_auction_lot_ids",
                return_value=["lot-1", "lot-2", "lot-3", "lot-4"],
            ),
        ):
            discovered = list(scraper.get_urls(skip=0))

        self.assertEqual(discovered, ["lot-2", "lot-4"])
        self.assertEqual(scraper._skipped_existing, 2)


if __name__ == "__main__":
    unittest.main()
