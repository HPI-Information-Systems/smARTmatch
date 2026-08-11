from __future__ import annotations

import json
import unittest

from scrapers.sothebys.parser import SothebysLotParser


class SothebysLotParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = SothebysLotParser()

    def test_parse_lot_response_extracts_core_fields(self) -> None:
        response = {
            "data": {
                "lot": {
                    "__typename": "LotV2",
                    "lotId": "lot-1",
                    "title": "Test Title",
                    "creatorsDisplayTitle": "Test Artist",
                    "description": "Description",
                    "catalogueNote": "Catalogue note",
                    "provenance": "Provenance",
                    "exhibition": "Exhibition",
                    "literature": "Literature",
                    "estimateV2": {
                        "__typename": "LowHighEstimateV2",
                        "lowEstimate": {"amount": "1000"},
                        "highEstimate": {"amount": "2000"},
                    },
                    "lotNumber": {"__typename": "VisibleLotNumber", "lotNumber": "12"},
                    "slug": "test-lot",
                    "auction": {
                        "auctionId": "auction-123",
                        "title": "Auction Title",
                        "location": "London",
                        "departmentNames": ["Modern", "Prints"],
                        "dates": {
                            "acceptsBids": "2026-01-02T10:00:00Z",
                            "goesLive": "2026-01-03T10:00:00Z",
                            "published": "2025-12-30T10:00:00Z",
                            "closed": "2026-01-04T10:00:00Z",
                        },
                        "slug": {"year": 2026, "name": "auction-name"},
                    },
                    "media": {
                        "images": [
                            {
                                "renditions": [
                                    {
                                        "imageSize": "Small",
                                        "width": 100,
                                        "height": 100,
                                        "url": "https://img/small.jpg",
                                    },
                                    {
                                        "imageSize": "Large",
                                        "width": 1000,
                                        "height": 1000,
                                        "url": "https://img/large.jpg",
                                    },
                                ]
                            }
                        ]
                    },
                }
            }
        }

        lot = self.parser.parse_lot_response(response)

        self.assertIsNotNone(lot)
        assert lot is not None
        self.assertEqual(lot.lot_id, "lot-1")
        self.assertEqual(lot.lot_number, "12")
        self.assertEqual(lot.lot_number_type, "VisibleLotNumber")
        self.assertEqual(lot.lot_number_visible, True)
        self.assertEqual(lot.artist_name, "Test Artist")
        self.assertEqual(lot.auction_date.isoformat(), "2026-01-03")
        self.assertEqual(lot.image_urls, ["https://img/large.jpg"])
        self.assertIn("Catalogue note", lot.description or "")
        self.assertIn("/auction-name/test-lot", lot.lot_url or "")
        self.assertEqual(lot.estimate_low, 1000.0)
        self.assertEqual(lot.estimate_high, 2000.0)
        self.assertEqual(lot.estimate_type, "LowHighEstimateV2")
        self.assertEqual(lot.estimate_upon_request, False)
        self.assertEqual(lot.auction_id, "auction-123")
        self.assertEqual(lot.auction_title, "Auction Title")
        self.assertEqual(lot.auction_location, "London")
        self.assertEqual(lot.auction_departments, "Modern, Prints")
        self.assertEqual(lot.auction_year, "2026")
        self.assertEqual(lot.auction_slug_name, "auction-name")

        raw = json.loads(lot.raw_data)
        self.assertEqual(raw["source"], "sothebys")
        self.assertEqual(raw["lot_response"]["data"]["lot"]["lotId"], "lot-1")

    def test_parse_lot_response_strips_html_from_text_fields(self) -> None:
        response = {
            "data": {
                "lot": {
                    "__typename": "LotV2",
                    "lotId": "lot-html",
                    "title": "<b>Test Title</b>",
                    "creatorsDisplayTitle": "<i>Test Artist</i>",
                    "description": "<p>Description</p>",
                    "catalogueNote": "<div>Catalogue note</div>",
                    "auction": {
                        "dates": {
                            "acceptsBids": "2026-01-02T10:00:00Z",
                        }
                    },
                }
            }
        }

        lot = self.parser.parse_lot_response(response)

        self.assertIsNotNone(lot)
        assert lot is not None
        self.assertEqual(lot.title, "Test Title")
        self.assertEqual(lot.artist_name, "Test Artist")
        self.assertEqual(lot.description, "Description\n\nCatalogue note")

    def test_parse_lot_response_handles_upon_request_and_hidden_lot_number(self) -> None:
        response = {
            "data": {
                "lot": {
                    "__typename": "LotV2",
                    "lotId": "lot-upr",
                    "title": "Upon request lot",
                    "estimateV2": {"__typename": "EstimateUponRequest"},
                    "lotNumber": {"__typename": "HiddenLotNumber"},
                    "auction": {
                        "dates": {"acceptsBids": "2026-01-02T10:00:00Z"},
                    },
                }
            }
        }

        lot = self.parser.parse_lot_response(response)
        self.assertIsNotNone(lot)
        assert lot is not None
        self.assertIsNone(lot.lot_number)
        self.assertEqual(lot.lot_number_type, "HiddenLotNumber")
        self.assertEqual(lot.lot_number_visible, False)
        self.assertIsNone(lot.estimate_low)
        self.assertIsNone(lot.estimate_high)
        self.assertEqual(lot.estimate_type, "EstimateUponRequest")
        self.assertEqual(lot.estimate_upon_request, True)

    def test_parse_lot_response_handles_missing_title_and_media(self) -> None:
        response = {
            "data": {
                "lot": {
                    "__typename": "LotV2",
                    "lotId": "lot-2",
                    "title": "  ",
                    "auction": {
                        "dates": {
                            "acceptsBids": "2026-01-02T10:00:00Z",
                        }
                    },
                }
            }
        }

        lot = self.parser.parse_lot_response(response)
        self.assertIsNotNone(lot)
        assert lot is not None
        self.assertEqual(lot.title, "Lot lot-2")
        self.assertEqual(lot.image_urls, [])
        self.assertEqual(lot.auction_date.isoformat(), "2026-01-02")

    def test_pick_best_rendition_prefers_size_then_area(self) -> None:
        renditions = [
            {"imageSize": "Large", "width": 600, "height": 600, "url": "https://img/a.jpg"},
            {"imageSize": "Large", "width": 800, "height": 800, "url": "https://img/b.jpg"},
            {"imageSize": "Medium", "width": 1000, "height": 1000, "url": "https://img/c.jpg"},
        ]

        chosen = self.parser._pick_best_rendition_url(renditions)
        self.assertEqual(chosen, "https://img/b.jpg")

    def test_parse_amount_handles_number_and_invalid(self) -> None:
        self.assertEqual(self.parser._parse_amount("12,500"), 12500.0)
        self.assertIsNone(self.parser._parse_amount("on request"))

    def test_hidden_lot_is_skipped(self) -> None:
        response = {"data": {"lot": {"__typename": "HiddenLot", "lotId": "hidden"}}}
        self.assertIsNone(self.parser.parse_lot_response(response))


if __name__ == "__main__":
    unittest.main()
