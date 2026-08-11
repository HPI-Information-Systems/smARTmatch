from __future__ import annotations

import json
import unittest

from scrapers.utils.auction_helpers import clean_whitespace, fit_varchar, json_dumps


class AuctionHelperTests(unittest.TestCase):
    def test_clean_whitespace(self) -> None:
        self.assertEqual(clean_whitespace("  A   B\nC  "), "A B C")
        self.assertIsNone(clean_whitespace("   \n\t"))
        self.assertIsNone(clean_whitespace(None))

    def test_fit_varchar_truncates(self) -> None:
        value = "x" * 20
        self.assertEqual(fit_varchar(value, max_len=10), "xxxxxxx...")

    def test_json_dumps_is_sorted_and_unicode(self) -> None:
        payload = {"b": 1, "a": "ä"}
        encoded = json_dumps(payload)
        self.assertEqual(json.loads(encoded), payload)
        self.assertTrue(encoded.index('"a"') < encoded.index('"b"'))


if __name__ == "__main__":
    unittest.main()
