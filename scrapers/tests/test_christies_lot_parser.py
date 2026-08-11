from __future__ import annotations

import unittest

from bs4 import BeautifulSoup

from scrapers.christies.lot_parser import ChristiesLotParser


class ChristiesLotParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ChristiesLotParser()

    def test_merge_data_prefers_non_empty_html_values(self) -> None:
        api_data = {
            "title_txt": "API title",
            "description": "API description",
            "sale": {"start_date": "2025-01-01"},
        }
        html_data = {
            "lots": {
                "title_txt": "HTML title",
                "description": "",
                "material": "Oil on canvas",
            },
            "sale": {"start_date": "2025-02-02"},
            "chr-specialist": {"name": "Specialist Name"},
        }

        lot_data, sale_data, specialist = self.parser.merge_data(api_data, html_data)

        self.assertEqual(lot_data["title_txt"], "HTML title")
        # Empty HTML values do not wipe non-empty API payload values.
        self.assertEqual(lot_data["description"], "API description")
        self.assertEqual(lot_data["material"], "Oil on canvas")
        self.assertEqual(sale_data["start_date"], "2025-02-02")
        self.assertEqual(specialist, {"name": "Specialist Name"})

    def test_parse_combined_page_title_splits_and_strips_suffix(self) -> None:
        artist, title = self.parser.parse_combined_page_title(
            "DAVID HILL, Portrait Study | Christie’s"
        )

        self.assertEqual(artist, "DAVID HILL")
        self.assertEqual(title, "Portrait Study")

    def test_lot_url_for_id_handles_online_only_and_standard(self) -> None:
        online_url = self.parser.lot_url_for_id("24333.1")
        standard_url = self.parser.lot_url_for_id("1234567")

        self.assertIn("onlineonly.christies.com", online_url)
        self.assertIn("objectid=24333.1", online_url)
        self.assertEqual(standard_url, "https://www.christies.com/en/lot/lot-1234567")

    def test_extract_product_json_ld_handles_lists(self) -> None:
        soup = BeautifulSoup(
            """
            <html>
              <head>
                <script type=\"application/ld+json\">[
                  {"@type": "BreadcrumbList"},
                  {"@type": "Product", "name": "Artist", "brand": "Work"}
                ]</script>
              </head>
            </html>
            """,
            "lxml",
        )

        product = self.parser.extract_product_json_ld(soup)

        self.assertIsNotNone(product)
        assert product is not None
        self.assertEqual(product.get("name"), "Artist")
        self.assertEqual(product.get("brand"), "Work")


if __name__ == "__main__":
    unittest.main()
