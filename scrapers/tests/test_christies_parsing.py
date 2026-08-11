from __future__ import annotations

import unittest

from bs4 import BeautifulSoup

from scrapers.christies.html import ChristiesHTMLScraper
from scrapers.christies.parser import build_auction_details, normalize_text
from scrapers.christies.scraper import ChristiesScraper


class ChristiesParsingTests(unittest.TestCase):
    def test_normalize_text_unescapes_entities(self) -> None:
        self.assertEqual(normalize_text("EUG&#200;NE ATGET &amp; Co."), "EUGÈNE ATGET & Co.")

    def test_placeholder_page_detection(self) -> None:
        scraper = ChristiesHTMLScraper()
        self.assertTrue(scraper._is_placeholder_page("Private Sales | What's available"))
        self.assertFalse(scraper._is_placeholder_page("Actual lot title"))

    def test_looks_like_object_title_ignores_artist_initials(self) -> None:
        scraper = ChristiesScraper(download_images=False)

        self.assertFalse(scraper._looks_like_object_title("A. R. Penck"))
        self.assertTrue(scraper._looks_like_object_title("A LARGE IZNIK POTTERY TILE"))

    def test_normalize_page_title_artist_for_swapped_object_title_lot(self) -> None:
        scraper = ChristiesScraper(download_images=False)
        title, artist, suppress_payload_artist = scraper._normalize_page_title_artist(
            lot_data={
                "title_primary_txt": "A FLORAL STUDY",
                "title_secondary_txt": "TIMURID IRAN, FIRST HALF 15TH CENTURY",
            },
            page_title="TIMURID IRAN, FIRST HALF 15TH CENTURY",
            page_artist="A FLORAL STUDY",
        )

        self.assertTrue(suppress_payload_artist)
        self.assertEqual(title, "A FLORAL STUDY")
        self.assertIsNone(artist)

    def test_normalize_page_title_artist_for_object_title_prefix(self) -> None:
        scraper = ChristiesScraper(download_images=False)
        title, artist, suppress_payload_artist = scraper._normalize_page_title_artist(
            lot_data={
                "title_primary_txt": "A LARGE IZNIK POTTERY TILE",
                "title_secondary_txt": "Ottoman collection lot",
            },
            page_title="Ottoman collection lot",
            page_artist="A LARGE IZNIK POTTERY TILE",
        )

        self.assertTrue(suppress_payload_artist)
        self.assertEqual(title, "A LARGE IZNIK POTTERY TILE")
        self.assertIsNone(artist)

    def test_normalize_page_title_artist_for_swapped_non_object_lot(self) -> None:
        scraper = ChristiesScraper(download_images=False)
        title, artist, suppress_payload_artist = scraper._normalize_page_title_artist(
            lot_data={
                "title_primary_txt": "WITH SIGNATURE OF XIA GUI (13TH-16TH CENTURY)",
                "title_secondary_txt": "Return from Fishing",
            },
            page_title="Return from Fishing",
            page_artist="WITH SIGNATURE OF XIA GUI (13TH-16TH CENTURY)",
        )

        self.assertTrue(suppress_payload_artist)
        self.assertEqual(title, "Return from Fishing")
        self.assertIsNone(artist)

    def test_resolve_artist_name_suppresses_object_title_candidates(self) -> None:
        scraper = ChristiesScraper(download_images=False)

        artist = scraper._resolve_artist_name(
            explicit_artist=None,
            page_artist="A LARGE IZNIK POTTERY TILE",
            payload_artist="A LARGE IZNIK POTTERY TILE",
            suppress_payload_artist=True,
        )

        self.assertIsNone(artist)

    def test_resolve_artist_name_prefers_explicit_artist(self) -> None:
        scraper = ChristiesScraper(download_images=False)

        artist = scraper._resolve_artist_name(
            explicit_artist="Claude Monet",
            page_artist="A LARGE IZNIK POTTERY TILE",
            payload_artist="A LARGE IZNIK POTTERY TILE",
            suppress_payload_artist=True,
        )

        self.assertEqual(artist, "Claude Monet")

    def test_extract_title_artist_from_online_only_markup(self) -> None:
        html = """
        <html>
          <head>
            <title>DAVID OCTAVIUS HILL (1802–1870) &amp; ROBERT ADAMSON (1821–1848),
              Lady Isabella Elizabeth (Norman) Grant. Wife of Sir Francis Grant, 1843-1847 | Christie’s</title>
            <meta property="og:title" content="DAVID OCTAVIUS HILL (1802–1870) &amp; ROBERT ADAMSON (1821–1848)" />
            <script type="application/ld+json">
              {
                "@context": "http://schema.org/",
                "@type": "Product",
                "name": "DAVID OCTAVIUS HILL (1802–1870) &amp; ROBERT ADAMSON (1821–1848)",
                "brand": "Lady Isabella Elizabeth (Norman) Grant. Wife of Sir Francis Grant, 1843-1847"
              }
            </script>
          </head>
          <body></body>
        </html>
        """
        soup = BeautifulSoup(html, "lxml")

        scraper = ChristiesScraper(download_images=False)
        title, artist = scraper._extract_title_artist_from_soup(soup)

        self.assertEqual(
            title,
            "Lady Isabella Elizabeth (Norman) Grant. Wife of Sir Francis Grant, 1843-1847",
        )
        self.assertEqual(
            artist,
            "DAVID OCTAVIUS HILL (1802–1870) & ROBERT ADAMSON (1821–1848)",
        )

    def test_html_details_extracts_online_only_accordion_content(self) -> None:
        html = """
        <html>
          <body>
            <div class="chr-accordion-item chr-accordion-item--expanded">
              <div class="chr-accordion-item__header">
                <div slot="header">Details</div>
              </div>
              <fieldset class="chr-accordion-item__container">
                <div class="content-zone chr-lot-section__accordion--content" slot="content">
                  DAVID OCTAVIUS HILL (1802–1870) &amp; ROBERT ADAMSON (1821–1848)
                  <br />
                  <i>Lady Isabella Elizabeth (Norman) Grant. Wife of Sir Francis Grant, 1843-1847</i>
                  <br />
                  calotype, mounted on paper, printed c. 1847
                </div>
              </fieldset>
            </div>
          </body>
        </html>
        """

        scraper = ChristiesHTMLScraper()
        details = scraper._extract_html_details(html)

        self.assertIn("Lady Isabella Elizabeth", details.get("description", ""))
        self.assertIn("calotype, mounted on paper", details.get("details", ""))

    def test_extract_lot_header_data_falls_back_to_html_details(self) -> None:
        html = """
        <html>
          <body>
            <h1 class="chr-lot-header__title">Fallback title</h1>
            <span class="chr-lot-header__artist-name">Fallback artist</span>
            <div class="chr-specialist-info">
              <span class="chr-specialist-info__name">Jane Doe</span>
            </div>
            <div class="chr-lot-section__accordion--content">Fallback description</div>
          </body>
        </html>
        """

        scraper = ChristiesHTMLScraper()
        payload = scraper._extract_lot_header_data(html)

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["lots"]["title_txt"], "Fallback title")
        self.assertEqual(payload["lots"]["artistName"], "Fallback artist")
        self.assertIn("Fallback description", payload["lots"]["description"])
        self.assertEqual(payload["chr-specialist"]["name"], "Jane Doe")

    def test_extract_chr_components_parses_embedded_json(self) -> None:
        html = 'window.chrComponents = {"lots": {"data": {"lots": [{"id": "1"}]}}};'
        scraper = ChristiesHTMLScraper()

        payload = scraper._extract_chr_components(html)
        self.assertIsInstance(payload, dict)
        assert payload is not None
        self.assertEqual(payload["lots"]["data"]["lots"][0]["id"], "1")

    def test_build_auction_details_parses_estimate_range_and_prices(self) -> None:
        lot_data = {
            "lot_id_txt": "29",
            "estimate_txt": "USD 2,000 - USD 3,000",
            "current_bid_txt": "USD 600",
            "price_realised_txt": "USD 1,200",
            "estimate_on_request": False,
            "price_on_request": False,
            "estimate_visible": True,
        }
        sale_data = {
            "sale_number": "24333",
            "sale_location": "New York",
            "start_date": "2026-04-03T11:00:00.000Z",
        }

        details = build_auction_details(lot_data, sale_data)

        self.assertEqual(details["lotNumber"], "29")
        self.assertEqual(details["estimateLow"], 2000.0)
        self.assertEqual(details["estimateHigh"], 3000.0)
        self.assertEqual(details["currentBid"], 600.0)
        self.assertEqual(details["realizedPrice"], 1200.0)
        self.assertEqual(details["estimateOnRequest"], False)

    def test_build_auction_details_marks_on_request_without_range(self) -> None:
        lot_data = {
            "lot_id_txt": "30",
            "estimate_txt": "Estimate on request",
            "estimate_on_request": True,
            "price_on_request": True,
        }

        details = build_auction_details(lot_data, {})

        self.assertEqual(details["lotNumber"], "30")
        self.assertIsNone(details.get("estimateLow"))
        self.assertIsNone(details.get("estimateHigh"))
        self.assertEqual(details["estimateOnRequest"], True)
        self.assertEqual(details["priceOnRequest"], True)


if __name__ == "__main__":
    unittest.main()
