from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from scrapers.drouot.scraper import DrouotScraper
from scrapers.lottissimo.parser import LottissimoLotParser
from scrapers.lottissimo.scraper import LottissimoScraper


class DrouotHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scraper = DrouotScraper(download_images=False)

    def test_derive_artist_and_title_keeps_clean_heading_without_inference(
        self,
    ) -> None:
        description = (
            "Louis-Michel VAN LOO, d'après\n"
            "Portrait présumé de l'architecte Soufflot à sa table de travail\n"
            "Huile sur toile\n"
            "80,5 x 64,5 cm"
        )

        title, artist = self.scraper._derive_artist_and_title(
            "35 - Louis-Michel VAN LOO, d'après",
            description,
        )

        self.assertEqual(title, "Louis-Michel VAN LOO, d'après")
        self.assertIsNone(artist)

    def test_placeholder_title_is_marked_as_unknown(self) -> None:
        self.assertTrue(
            self.scraper._parser._is_placeholder_title("NICHT IDENTIFIZIERTE SIGNATUR")
        )
        self.assertTrue(
            self.scraper._parser._is_placeholder_title("Signature non identifiée")
        )
        self.assertFalse(
            self.scraper._parser._is_placeholder_title("Portrait d'une femme")
        )

    def test_resolve_title_and_artist_uses_only_explicit_artist_fields(self) -> None:
        title, artist = self.scraper._parser._resolve_title_and_artist(
            raw_heading="35 - Nature morte",
            description="ignored",
            description_from_dom="ignored",
            lot_object='artistName:"Paul Klee"',
            lot_url="https://drouot.com/de/l/35-example",
        )

        self.assertEqual(title, "Nature morte")
        self.assertEqual(artist, "Paul Klee")

    def test_resolve_title_and_artist_nulls_unknown_title_and_artist(self) -> None:
        title, artist = self.scraper._parser._resolve_title_and_artist(
            raw_heading="17 - NICHT IDENTIFIZIERTE SIGNATUR",
            description="ignored",
            description_from_dom="ignored",
            lot_object='artistName:"undefined"',
            lot_url="https://drouot.com/de/l/17-example",
        )

        self.assertIsNone(title)
        self.assertIsNone(artist)

    def test_build_raw_payload_keeps_lot_object_and_schema(self) -> None:
        raw_data = json.loads(
            self.scraper._build_raw_payload(
                lot_url="https://drouot.example/lot-1",
                lot_object="{id:1,title:'Example'}",
                product_schema={"@type": "Product", "name": "Example"},
                display_heading="35 - Example",
            )
        )

        self.assertEqual(raw_data["lot_object"], "{id:1,title:'Example'}")
        self.assertEqual(raw_data["structured_product"]["name"], "Example")
        self.assertEqual(raw_data["display_heading"], "35 - Example")

    def test_normalize_heading_text_truncates_overlong_h1(self) -> None:
        long_heading = (
            "Walter Sickert (1860-1942) Cliffs at Dieppe Signed Oil on canvas board. "
            + "Provenance details. " * 40
        )

        normalized = self.scraper._parser._normalize_heading_text(long_heading)

        self.assertTrue(normalized.startswith("Walter Sickert"))
        self.assertLessEqual(len(normalized), 220)

    def test_extract_description_from_dom_prefers_lot_text_over_boilerplate(
        self,
    ) -> None:
        soup = BeautifulSoup(
            """
            <html>
              <body>
                <p>Le prix de réserve est le prix minimum accepté par le vendeur.</p>
                <p class="whitespace-pre-line">Louis-Michel VAN LOO, d'après\nPortrait\nHuile sur toile</p>
              </body>
            </html>
            """,
            "lxml",
        )

        description = self.scraper._extract_description_from_dom(soup)

        self.assertIn("Louis-Michel VAN LOO", description)
        self.assertNotIn("prix de réserve", description.casefold())

    def test_get_urls_without_max_pages_stops_on_empty_page(self) -> None:
        page_one = '<a href="/de/l/111-lot-a"></a><a href="/de/l/112-lot-b"></a>'
        page_two = '<a href="/de/l/113-lot-c"></a>'
        page_three = "<html><body>No lots</body></html>"

        with patch.object(
            self.scraper, "fetch_html", side_effect=[page_one, page_two, page_three]
        ) as fetch_html:
            urls = list(self.scraper.get_urls())

        self.assertEqual(
            urls,
            [
                "https://drouot.com/de/l/111-lot-a",
                "https://drouot.com/de/l/112-lot-b",
                "https://drouot.com/de/l/113-lot-c",
            ],
        )
        self.assertEqual(fetch_html.call_count, 3)

    def test_get_urls_respects_explicit_max_pages(self) -> None:
        scraper = DrouotScraper(download_images=False, max_pages=1)
        page_one = '<a href="/de/l/111-lot-a"></a><a href="/de/l/112-lot-b"></a>'
        page_two = '<a href="/de/l/113-lot-c"></a>'

        with patch.object(
            scraper, "fetch_html", side_effect=[page_one, page_two]
        ) as fetch_html:
            urls = list(scraper.get_urls())

        self.assertEqual(
            urls,
            [
                "https://drouot.com/de/l/111-lot-a",
                "https://drouot.com/de/l/112-lot-b",
            ],
        )
        self.assertEqual(fetch_html.call_count, 1)


class LottissimoHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scraper = LottissimoScraper()
        self.parser = LottissimoLotParser()

    def test_split_title_and_artist_does_not_infer_from_hyphen_suffix(self) -> None:
        title, artist = self.parser._split_title_and_artist(
            "Mutter mit Jungen - Käthe Kollwitz"
        )

        self.assertEqual(title, "Mutter mit Jungen - Käthe Kollwitz")
        self.assertIsNone(artist)

    def test_split_title_and_artist_defaults_to_null_for_comma_delimited_title(
        self,
    ) -> None:
        title, artist = self.parser._split_title_and_artist(
            "Beutner, Johannes. Drei Grazien"
        )

        self.assertIsNone(title)
        self.assertIsNone(artist)

    def test_parse_lot_page_does_not_infer_artist_from_description(self) -> None:
        html = """
        <html>
          <body>
            <h1 class="header header-lot-title">Münchener Oktoberfest 1875</h1>
            <div class="ui bottom attached active tab segment" data-tab="description">
              nach einer Skizze von Julius Adam (* 1852 München)
            </div>
            <div class="ui bottom attached tab segment" data-tab="auction">Auction details</div>
            <div class="touch-swipe-gallery"></div>
          </body>
        </html>
        """

        page = self.parser.parse_lot_page(
            html=html,
            lot_url="https://www.lot-tissimo.com/de-de/auction-catalogues/example/catalogue-id-example/lot-abc-1",
        )

        self.assertIsNotNone(page)
        assert page is not None
        self.assertEqual(page.title, "Münchener Oktoberfest 1875")
        self.assertIsNone(page.artist_name)

    def test_parse_lot_page_defaults_title_to_null_for_comma_delimited_heading(
        self,
    ) -> None:
        html = """
        <html>
          <body>
            <h1 class="header header-lot-title">Peeter Boel, Umkreis, Wildschweinjagd</h1>
            <div class="ui bottom attached active tab segment" data-tab="description">Beschreibung</div>
            <div class="ui bottom attached tab segment" data-tab="auction">Auction details</div>
            <div class="touch-swipe-gallery"></div>
          </body>
        </html>
        """

        page = self.parser.parse_lot_page(
            html=html,
            lot_url="https://www.lot-tissimo.com/de-de/auction-catalogues/example/catalogue-id-example/lot-abc-1",
        )

        self.assertIsNotNone(page)
        assert page is not None
        self.assertIsNone(page.title)

    def test_parse_lot_page_prefers_description_tab_segment_over_menu_label(
        self,
    ) -> None:
        html = """
        <html>
          <body>
            <div class="ui top attached tabular menu">
              <a class="active item" data-tab="description">Beschreibung</a>
              <a class="item" data-tab="auction">Auktion</a>
            </div>
            <div class="ui bottom attached active tab segment" data-tab="description">
              <p>Öl auf Leinwand.</p>
              <p>Signiert unten rechts.</p>
            </div>
            <div class="ui bottom attached tab segment" data-tab="auction">Auktionsdetails</div>
            <h1 class="header header-lot-title">Beispielbild</h1>
            <div class="touch-swipe-gallery"></div>
          </body>
        </html>
        """

        page = self.parser.parse_lot_page(
            html=html,
            lot_url="https://www.lot-tissimo.com/de-de/auction-catalogues/example/catalogue-id-example/lot-abc-2",
        )

        self.assertIsNotNone(page)
        assert page is not None
        self.assertEqual(page.description, "Öl auf Leinwand.\nSigniert unten rechts.")

    def test_parse_lot_page_extracts_provenance_from_description(self) -> None:
        html = """
        <html>
          <body>
            <h1 class="header header-lot-title">Beispielbild</h1>
            <div class="ui bottom attached active tab segment" data-tab="description">
              Öl auf Leinwand.<br />
              Signiert unten rechts.<br /><br />
              Provenienz<br />
              Sammlung A. - Sammlung B.<br /><br />
              Literatur siehe Werkverzeichnis.
            </div>
            <div class="ui bottom attached tab segment" data-tab="auction">Auktionsdetails</div>
            <div class="touch-swipe-gallery"></div>
          </body>
        </html>
        """

        page = self.parser.parse_lot_page(
            html=html,
            lot_url="https://www.lot-tissimo.com/de-de/auction-catalogues/example/catalogue-id-example/lot-abc-2",
        )

        self.assertIsNotNone(page)
        assert page is not None
        self.assertEqual(
            page.description,
            "Öl auf Leinwand.\nSigniert unten rechts.\nLiteratur siehe Werkverzeichnis.",
        )
        self.assertEqual(page.provenance, "Sammlung A. - Sammlung B.")

    def test_extract_datalayer_push_object_handles_trailing_commas(self) -> None:
        html = """
        <script>
          window.dataLayer = window.dataLayer || [];
          window.dataLayer.push({"lotId":"123","artistName":"X",});
        </script>
        """

        payload = self.parser._extract_datalayer_push_object(html)
        self.assertEqual(payload, '{"lotId":"123","artistName":"X"}')

    def test_parse_auctioneer_page_extracts_contact_fields(self) -> None:
        html = """
        <div class="auction-summary auctioneers-landing-page">
          <span itemprop="name">Test Auctioneer</span>
          <span itemprop="streetAddress">Street 1</span>
          <span itemprop="addressLocality">Berlin</span>
          <span itemprop="addressRegion">BE</span>
          <span itemprop="postalCode">10115</span>
          <span itemprop="addressCountry">DE</span>
          <span class="phone details">+49 30 12345</span>
          <a href="mailto:test@example.org">mail</a>
        </div>
        """

        name, address, phone, email = self.parser.parse_auctioneer_page(html)
        self.assertEqual(name, "Test Auctioneer")
        self.assertIn("Street 1", address)
        self.assertIn("Berlin", address)
        self.assertEqual(phone, "+49 30 12345")
        self.assertEqual(email, "test@example.org")

    def test_parse_lot_page_extracts_lot_number_and_typed_auction_fields(self) -> None:
        html = """
        <html>
          <head>
            <script>
              window.dataLayer = window.dataLayer || [];
              window.dataLayer.push({
                "lotId":"uuid-1",
                "lotName":"Mutter mit Jungen - Käthe Kollwitz",
                "lotDescription":"2046",
                "minEstimate":"100,00",
                "maxEstimate":"200,00",
                "openingPrice":"50,00",
                "currentWatchers":"3",
                "currentBids":"1",
                "lotImageCount":"1",
                "deliveryAvailable":"True",
                "featuredLot":"False",
                "hasLotDescription":"True",
                "lotStartDate":"2026-05-01",
                "auctionCity":"Berlin",
                "auctionCountry":"Germany",
                "auctioneer":"Test Haus"
              });
            </script>
          </head>
          <body>
            <div class="lot-details"><p class="lot-number">Los 2046</p></div>
            <h1 class="header header-lot-title">Mutter mit Jungen - Käthe Kollwitz</h1>
            <div class="ui bottom attached active tab segment" data-tab="description">Beschreibung</div>
            <div class="ui bottom attached tab segment" data-tab="auction">Auction details tab</div>
            <div class="touch-swipe-gallery">
              <img src="https://cdn.globalauctionplatform.com/auction/img1.jpg?w=155&amp;h=155" />
            </div>
          </body>
        </html>
        """

        page = self.parser.parse_lot_page(
            html=html,
            lot_url="https://www.lot-tissimo.com/de-de/auction-catalogues/example/catalogue-id-example/lot-fallback-id",
        )

        self.assertIsNotNone(page)
        assert page is not None
        self.assertEqual(page.lot_id, "uuid-1")
        self.assertEqual(page.lot_number, "2046")
        self.assertEqual(page.title, "Mutter mit Jungen - Käthe Kollwitz")
        self.assertIsNone(page.artist_name)
        self.assertEqual(page.auctioneer_name, "Test Haus")
        self.assertEqual(
            page.image_urls, ["https://cdn.globalauctionplatform.com/auction/img1.jpg"]
        )

        auction_details = json.loads(page.auction_details)
        self.assertEqual(auction_details["lotNumber"], "2046")
        self.assertEqual(auction_details["estimateLow"], 100.0)
        self.assertEqual(auction_details["estimateHigh"], 200.0)
        self.assertEqual(auction_details["startPrice"], 50.0)
        self.assertNotIn("minEstimate", auction_details)
        self.assertNotIn("maxEstimate", auction_details)
        self.assertNotIn("openingPrice", auction_details)
        self.assertEqual(auction_details["currentWatchers"], 3)
        self.assertEqual(auction_details["currentBids"], 1)
        self.assertEqual(auction_details["deliveryAvailable"], True)

    def test_extract_images_from_current_lot_image_markup(self) -> None:
        html = """
        <div class="lot-image">
          <img
            data-zoom-url="https://cdn.example.test/images/one.jpg"
            src="https://cdn.example.test/images/one.jpg?w=1080&amp;h=720"
          />
          <img
            data-src="https://cdn.example.test/images/two.jpg?w=1080&amp;h=720"
          />
          <img src="https://cdn.example.test/images/one.jpg?w=80&amp;h=80" />
        </div>
        <img src="https://cdn.example.test/recommendation.jpg" />
        """

        soup = BeautifulSoup(html, "lxml")
        self.assertEqual(
            self.parser._extract_image_urls(soup),
            [
                "https://cdn.example.test/images/one.jpg",
                "https://cdn.example.test/images/two.jpg",
            ],
        )

    def test_parse_lot_page_omits_zero_estimates_but_keeps_opening_price(self) -> None:
        html = """
        <html>
          <head>
            <script>
              window.dataLayer = window.dataLayer || [];
              window.dataLayer.push({
                "lotId":"uuid-2",
                "lotName":"Heckendorf, Franz München",
                "lotDescription":"34",
                "minEstimate":"0,00",
                "maxEstimate":"0,00",
                "openingPrice":"380,00",
                "lotStartDate":"2026-05-09",
                "auctioneer":"Auktionshaus Satow"
              });
            </script>
          </head>
          <body>
            <div class="ui bottom attached active tab segment" data-tab="description">Beschreibung</div>
            <div class="ui bottom attached tab segment" data-tab="auction">Auction details tab</div>
          </body>
        </html>
        """

        page = self.parser.parse_lot_page(
            html=html,
            lot_url="https://www.lot-tissimo.com/de-de/auction-catalogues/satow/catalogue-id-satow10062/lot-fallback-id",
        )

        self.assertIsNotNone(page)
        assert page is not None
        auction_details = json.loads(page.auction_details)
        self.assertEqual(auction_details["startPrice"], 380.0)
        self.assertNotIn("minEstimate", auction_details)
        self.assertNotIn("maxEstimate", auction_details)
        self.assertNotIn("openingPrice", auction_details)
        self.assertNotIn("estimateLow", auction_details)
        self.assertNotIn("estimateHigh", auction_details)

    def test_parse_lot_page_omits_zero_optional_counters(self) -> None:
        html = """
        <html>
          <head>
            <script>
              window.dataLayer = window.dataLayer || [];
              window.dataLayer.push({
                "lotId":"uuid-3",
                "lotName":"Untitled",
                "currentWatchers":"0",
                "currentBids":"0",
                "lotImageCount":"0"
              });
            </script>
          </head>
          <body>
            <div class="ui bottom attached active tab segment" data-tab="description">Beschreibung</div>
            <div class="ui bottom attached tab segment" data-tab="auction">Auction details tab</div>
          </body>
        </html>
        """

        page = self.parser.parse_lot_page(
            html=html,
            lot_url="https://www.lot-tissimo.com/de-de/auction-catalogues/example/catalogue-id-example/lot-fallback-id",
        )

        self.assertIsNotNone(page)
        assert page is not None
        auction_details = json.loads(page.auction_details)
        self.assertNotIn("currentWatchers", auction_details)
        self.assertNotIn("currentBids", auction_details)
        self.assertNotIn("lotImageCount", auction_details)

    def test_extract_auction_metadata_uses_lot_end_date_when_start_missing(
        self,
    ) -> None:
        auction_date, city, country = self.parser._extract_auction_metadata(
            "",
            {
                "lotStartDate": "",
                "lotEndDate": "2026-05-12",
                "auctionCity": "Hamburg",
                "auctionCountry": "Germany",
            },
        )

        self.assertIsNotNone(auction_date)
        assert auction_date is not None
        self.assertEqual(auction_date.isoformat(), "2026-05-12")
        self.assertEqual(city, "Hamburg")
        self.assertEqual(country, "Germany")

    def test_get_urls_applies_skip_globally(self) -> None:
        page_one = """
        <a href="/de-de/auction-catalogues/example/catalogue-id-example/lot-a"></a>
        <a href="/de-de/auction-catalogues/example/catalogue-id-example/lot-b"></a>
        """
        page_two = """
        <a href="/de-de/auction-catalogues/example/catalogue-id-example/lot-c"></a>
        """

        with (
            patch.object(self.scraper, "_get_page_urls", return_value=["p1", "p2"]),
            patch.object(self.scraper, "fetch_html", side_effect=[page_one, page_two]),
        ):
            urls = list(self.scraper.get_urls(skip=1))

        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].endswith("lot-b"))
        self.assertTrue(urls[1].endswith("lot-c"))

    def test_get_urls_stops_at_max_lots(self) -> None:
        self.scraper.max_lots = 2
        page_one = """
        <a href="/de-de/auction-catalogues/example/catalogue-id-example/lot-a"></a>
        <a href="/de-de/auction-catalogues/example/catalogue-id-example/lot-b"></a>
        <a href="/de-de/auction-catalogues/example/catalogue-id-example/lot-c"></a>
        """

        with (
            patch.object(self.scraper, "_get_page_urls", return_value=["p1", "p2"]),
            patch.object(
                self.scraper, "fetch_html", return_value=page_one
            ) as fetch_html,
        ):
            urls = list(self.scraper.get_urls(skip=0))

        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].endswith("lot-a"))
        self.assertTrue(urls[1].endswith("lot-b"))
        fetch_html.assert_called_once()

    def test_extract_lot_urls_filters_and_normalizes_links(self) -> None:
        html = """
        <a href="/de-de/auction-catalogues/example/catalogue-id-example/lot-a"></a>
        <a href="/de-de/auction-catalogues/example/catalogue-id-example/lot-b?ref=123"></a>
        <a href="/de-de/not-a-lot"></a>
        """

        urls = self.parser.extract_lot_urls(html)

        self.assertEqual(
            urls,
            [
                "https://www.lot-tissimo.com/de-de/auction-catalogues/example/catalogue-id-example/lot-a",
                "https://www.lot-tissimo.com/de-de/auction-catalogues/example/catalogue-id-example/lot-b",
            ],
        )

    def test_build_raw_payload_keeps_datalayer_and_meta(self) -> None:
        soup = BeautifulSoup(
            """
            <html>
              <head>
                <title>Example title</title>
                <meta property="og:title" content="Example og title" />
                <meta property="og:description" content="Example og description" />
              </head>
              <body>
                <div id="description">desc</div>
                <div id="auction">auction</div>
              </body>
            </html>
            """,
            "lxml",
        )
        raw_data = json.loads(
            self.parser._build_raw_payload(
                soup=soup,
                lot_url="https://lot.example/1",
                raw_title="Work - Artist",
                data_layer_payload='{"lotId":"1"}',
                data_layer={"lotId": "1"},
                description_tag=soup.find(id="description"),
                auction_details_tag=soup.find(id="auction"),
            )
        )

        self.assertEqual(raw_data["data_layer_raw"], '{"lotId":"1"}')
        self.assertEqual(raw_data["data_layer"]["lotId"], "1")
        self.assertEqual(raw_data["og_title"], "Example og title")
        self.assertIn("desc", raw_data["description_html"])


if __name__ == "__main__":
    unittest.main()
