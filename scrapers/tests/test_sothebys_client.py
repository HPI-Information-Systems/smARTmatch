from __future__ import annotations

import unittest
from urllib.parse import parse_qsl, urlsplit

from scrapers.sothebys.client import SothebysClient


class SothebysClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SothebysClient(min_wait=0.0, max_wait=0.0)

    def test_calendar_page_url_preserves_repeated_filters(self) -> None:
        base = "https://www.sothebys.com/en/calendar?f4=a&f4=b&p=5&_requestType=ajax"

        page_url = self.client.calendar_page_url(base, page_number=2)

        pairs = parse_qsl(urlsplit(page_url).query, keep_blank_values=True)
        self.assertEqual([value for key, value in pairs if key == "f4"], ["a", "b"])
        self.assertEqual([value for key, value in pairs if key == "p"], ["2"])
        self.assertEqual([value for key, value in pairs if key == "_requestType"], ["ajax"])

    def test_extract_buy_links_deduplicates_and_expands(self) -> None:
        html = """
        <a href="/en/buy/auction/2026/one">One</a>
        <a href="/en/buy/auction/2026/one">One duplicate</a>
        <a href="/en/buy/auction/2026/two">Two</a>
        <a href="/en/sell">Not buy</a>
        """

        links = self.client.extract_buy_links(html, base_url="https://www.sothebys.com/en/calendar")

        self.assertEqual(
            links,
            [
                "https://www.sothebys.com/en/buy/auction/2026/one",
                "https://www.sothebys.com/en/buy/auction/2026/two",
            ],
        )

    def test_extract_auction_id_from_next_data(self) -> None:
        cache_payload = {
            "props": {
                "pageProps": {
                    "apolloCache": {
                        "ROOT_QUERY": {},
                        "Auction:123e4567-e89b-12d3-a456-426614174000": {
                            "__typename": "Auction",
                            "id": "123e4567-e89b-12d3-a456-426614174000",
                        },
                    }
                }
            }
        }
        html = (
            "<html><body>"
            '<script id="__NEXT_DATA__" type="application/json">'
            + __import__("json").dumps(cache_payload)
            + "</script></body></html>"
        )

        auction_id = self.client.extract_auction_id(html)

        self.assertEqual(auction_id, "123e4567-e89b-12d3-a456-426614174000")

    def test_extract_auction_id_decodes_apollo_relay_id(self) -> None:
        cache_payload = {
            "props": {
                "pageProps": {
                    "apolloCache": {
                        "Auction:QXVjdGlvbl9mMGVlYTc4OS1kNGM1LTRjOTctYmQzOC1mMDQyN2Q3Y2Q3YTQ=": {
                            "__typename": "Auction"
                        }
                    }
                }
            }
        }
        html = (
            "<html><body>"
            '<script id="__NEXT_DATA__" type="application/json">'
            + __import__("json").dumps(cache_payload)
            + "</script></body></html>"
        )

        auction_id = self.client.extract_auction_id(html)

        self.assertEqual(auction_id, "f0eea789-d4c5-4c97-bd38-f0427d7cd7a4")

    def test_extract_auction_id_skips_malformed_cache_key(self) -> None:
        cache_payload = {
            "props": {
                "pageProps": {
                    "apolloCache": {
                        "Auction:☃": {"__typename": "Auction"},
                        "Auction:123e4567-e89b-12d3-a456-426614174000": {
                            "__typename": "Auction"
                        },
                    }
                }
            }
        }
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + __import__("json").dumps(cache_payload)
            + "</script>"
        )

        self.assertEqual(
            self.client.extract_auction_id(html),
            "123e4567-e89b-12d3-a456-426614174000",
        )

    def test_extract_auction_id_raises_when_next_data_missing(self) -> None:
        with self.assertRaises(ValueError):
            self.client.extract_auction_id("<html><body>nope</body></html>")

    def test_fetch_calendar_html_extracts_html_from_json_payload(self) -> None:
        class _Response:
            text = '{"html": "<div>calendar</div>"}'

            @staticmethod
            def raise_for_status() -> None:
                return None

        class _Session:
            @staticmethod
            def get(*args, **kwargs):
                return _Response()

        self.client.session = _Session()
        html = self.client.fetch_calendar_html("https://www.sothebys.com/en/calendar?p=1")
        self.assertEqual(html, "<div>calendar</div>")


if __name__ == "__main__":
    unittest.main()
