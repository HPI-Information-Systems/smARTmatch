from __future__ import annotations

import unittest

from scrapers.christies.transform import resolve_lot_fields, should_log_resolution_debug


class ChristiesTransformTests(unittest.TestCase):
    def test_resolve_lot_fields_prefers_page_title_and_extracts_dimensions(self) -> None:
        lot_data = {
            "title_txt": "Payload Title",
            "title_primary_txt": "Artist Name",
            "description": "Description",
            "material": "Oil",
            "technique": "Brush",
            "date": "1900",
            "condition": "Good",
            "signature": "Signed",
            "literature": "Literature",
            "width": 12.5,
            "height": 24.0,
        }
        sale_data = {"startDate": "2026-05-10T10:00:00Z"}

        fields = resolve_lot_fields(
            lot_id="123",
            lot_data=lot_data,
            sale_data=sale_data,
            page_title="Web Title",
        )

        self.assertEqual(fields.title, "Web Title")
        self.assertEqual(fields.payload_artist, "Artist Name")
        self.assertEqual(fields.description, "Description")
        self.assertEqual(fields.width, 12.5)
        self.assertEqual(fields.height, 24.0)
        self.assertEqual(fields.auction_date.isoformat(), "2026-05-10")

    def test_should_log_resolution_debug(self) -> None:
        self.assertTrue(should_log_resolution_debug(title="Christie's lot 123", artist_name="Artist"))
        self.assertTrue(should_log_resolution_debug(title="Resolved Title", artist_name=None))
        self.assertFalse(should_log_resolution_debug(title="Resolved Title", artist_name="Artist"))


if __name__ == "__main__":
    unittest.main()
