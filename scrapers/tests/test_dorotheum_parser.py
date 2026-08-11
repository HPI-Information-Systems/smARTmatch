from __future__ import annotations

import json
import unittest
from datetime import date

from scrapers.dorotheum.models import DorotheumListingLot
from scrapers.dorotheum.parser import DorotheumLotParser


class DorotheumLotParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = DorotheumLotParser()

    def test_extract_listing_lots_from_embedded_script(self) -> None:
        html = """
        <script>
            var showWarengruppenFilter = true;
            var initialSort = 'endtimeASC';
            var lots = {
                "100": {
                    "uid": 100,
                    "titel": "Christian Frank *",
                    "kuenstlername": "Christian Frank",
                    "beschreibung": "Ohne Titel",
                    "auctionTypeIdentifier": "online",
                    "datum": 1777276800,
                    "ablaufzeit": 1777277220,
                    "auktion": "Kunst, Antiquitäten, Möbel und Technik",
                    "filiale": 2,
                    "publicNummer": "110-025750/0010",
                    "detailURL": "/de/l/10091143/",
                    "images400x400": ["/fileadmin/example.webp"],
                    "preisFloat": 1500,
                    "warengruppeTitel": "20. Jahrhundert",
                    "currency": "EUR"
                }
            };
            var filialen = [{"uid": 2, "name": "Wien | Favoriten"}];
        </script>
        """

        lots = self.parser.extract_listing_lots(html)
        self.assertEqual(len(lots), 1)

        lot = lots[0]
        self.assertEqual(lot.uid, "100")
        self.assertEqual(lot.lot_id, "110-025750/0010")
        self.assertEqual(lot.lot_url, "https://www.dorotheum.com/de/l/10091143/")
        self.assertEqual(lot.artist_name, "Christian Frank")
        self.assertIsNone(lot.title)
        self.assertEqual(lot.auction_location, "Wien | Favoriten")
        self.assertEqual(lot.start_price, 1500.0)
        self.assertEqual(lot.currency, "EUR")
        self.assertEqual(lot.auction_date.isoformat(), "2026-04-27")

    def test_extract_listing_lot_nulls_title_when_heading_matches_artist_variant(self) -> None:
        html = """
        <script>
            var lots = {
                "100": {
                    "uid": 100,
                    "titel": "Robert Voit (Foit) *",
                    "kuenstlername": "Robert Voit",
                    "beschreibung": "Bunter Blumenstrauß",
                    "detailURL": "/de/l/10091143/"
                }
            };
            var filialen = [];
        </script>
        """

        lots = self.parser.extract_listing_lots(html)
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0].artist_name, "Robert Voit")
        self.assertIsNone(lots[0].title)

    def test_extract_lot_urls_falls_back_to_anchor_links(self) -> None:
        html = """
        <a href="/de/l/10091143/">Lot A</a>
        <a href="/de/l/10091143/">Lot A dup</a>
        <a href="/de/l/10091251/">Lot B</a>
        <a href="/de/c/other/">ignore</a>
        """

        urls = self.parser.extract_lot_urls(html)
        self.assertEqual(
            urls,
            [
                "https://www.dorotheum.com/de/l/10091143/",
                "https://www.dorotheum.com/de/l/10091251/",
            ],
        )

    def test_parse_lot_page_extracts_structured_fields(self) -> None:
        html = """
        <script type="application/ld+json" id="ext-schema-jsonld">
        {
          "@context": "https://schema.org/",
          "@graph": [
            {
              "@type": "Painting",
              "headline": "Christian Frank *",
              "image": ["https://www.dorotheum.com/fileadmin/lot-images/10A260427/400x400/christian-frank-10091143.webp"]
            },
            {
              "@type": "Person",
              "name": "Ulrich Prinz",
              "telephone": "+43-1-515 60-291",
              "email": "ulrich.prinz@dorotheum.at"
            }
          ]
        }
        </script>
        <div class="lot-status"
             data-rufpreis="1500"
             data-currency="EUR"
             data-ga4-tracking-data="{&quot;auction_name&quot;:&quot;Kunst, Antiquitäten, Möbel und Technik&quot;,&quot;auction_location&quot;:&quot;Wien | Favoriten&quot;,&quot;lot_id&quot;:&quot;110-025750/0010&quot;,&quot;lot_artist&quot;:&quot;undefined&quot;,&quot;lot_currency&quot;:&quot;EUR&quot;,&quot;lot_category&quot;:&quot;20. Jahrhundert&quot;}">
        </div>
        <p class="headline headline--h2-alike margin--0 inline-block">Lot Nr. 110-025750/0010</p>
        <h1 class="headline"><span class="lot-title-tooltip">Christian Frank *</span></h1>
        <div id="auktion-details">
          <time datetime="2026-04-27 10:07">27.04.2026 - 10:07</time>
          <dl>
            <dt>Schätzwert:</dt>
            <dd>EUR 1.000,- bis EUR 1.500,-</dd>
          </dl>
        </div>
        <table id="auction-details-expanded" class="auction-details-table">
          <tr><th>Auktion:</th><td>Kunst, Antiquitäten, Möbel und Technik</td></tr>
          <tr><th>Auktionstyp:</th><td>Online Auction</td></tr>
          <tr><th>Auktionsort:</th><td>Wien | Favoriten</td></tr>
        </table>
        <div class="lot-gallery-container"
             data-json="[{&quot;hires&quot;:&quot;/fileadmin/lot-images/10A260427/hires/christian-frank-10091143.jpg&quot;}]"></div>
        <div id="email-share-form" data-beschreibung=", ohne Titel" data-datum="27.04.2026 - 10:07"></div>
        """

        lot = self.parser.parse_lot_page(html=html, lot_url="https://www.dorotheum.com/de/l/10091143/")

        self.assertIsNotNone(lot)
        assert lot is not None
        self.assertEqual(lot.lot_uid, "10091143")
        self.assertEqual(lot.lot_id, "110-025750/0010")
        self.assertIsNone(lot.title)
        self.assertIsNone(lot.artist_name)
        self.assertEqual(lot.start_price, 1500.0)
        self.assertEqual(lot.estimate_low, 1000.0)
        self.assertEqual(lot.estimate_high, 1500.0)
        self.assertEqual(lot.currency, "EUR")
        self.assertEqual(lot.auction_name, "Kunst, Antiquitäten, Möbel und Technik")
        self.assertEqual(lot.auction_type, "Online Auction")
        self.assertEqual(lot.auction_location, "Wien | Favoriten")
        self.assertEqual(lot.auction_date.isoformat(), "2026-04-27")
        self.assertEqual(lot.expert_name, "Ulrich Prinz")
        self.assertEqual(lot.expert_email, "ulrich.prinz@dorotheum.at")
        self.assertEqual(
            lot.image_urls,
            ["https://www.dorotheum.com/fileadmin/lot-images/10A260427/hires/christian-frank-10091143.jpg"],
        )

        raw = json.loads(lot.raw_data)
        self.assertEqual(raw["source"], "dorotheum")
        self.assertEqual(raw["ga4_tracking"]["lot_id"], "110-025750/0010")

    def test_parse_lot_page_prefers_explicit_listing_artist_without_reusing_as_title(self) -> None:
        html = """
        <div class="lot-status"
             data-ga4-tracking-data="{&quot;lot_id&quot;:&quot;110-025750/0010&quot;,&quot;lot_artist&quot;:&quot;undefined&quot;}"></div>
        <p class="headline">Lot Nr. 110-025750/0010</p>
        <h1 class="headline"><span class="lot-title-tooltip">Christian Frank *</span></h1>
        """

        listing_lot = DorotheumListingLot(
            uid="10091143",
            lot_id="110-025750/0010",
            lot_url="https://www.dorotheum.com/de/l/10091143/",
            title=None,
            artist_name="Christian Frank",
            description="",
            auction_name="Auktion",
            auction_type="online",
            auction_location="Wien",
            auction_date=date(2026, 4, 27),
            auction_end_timestamp=None,
            currency="EUR",
            start_price=1500.0,
            lot_category="20. Jahrhundert",
            image_urls=[],
            raw_data={},
        )

        lot = self.parser.parse_lot_page(
            html=html,
            lot_url="https://www.dorotheum.com/de/l/10091143/",
            listing_lot=listing_lot,
        )

        self.assertIsNotNone(lot)
        assert lot is not None
        self.assertEqual(lot.artist_name, "Christian Frank")
        self.assertIsNone(lot.title)


if __name__ == "__main__":
    unittest.main()
