from __future__ import annotations

import unittest
from unittest.mock import patch

from scrapers.dorotheum.scraper import DorotheumScraper


class _FakeDB:
    def __init__(self) -> None:
        self.session = object()


class DorotheumHttpTests(unittest.TestCase):
    def _build_scraper(self, **kwargs) -> DorotheumScraper:
        return DorotheumScraper(
            db=_FakeDB(),
            min_wait=0.0,
            max_wait=0.0,
            request_max_retries=1,
            **kwargs,
        )

    def test_parse_cookie_header_ignores_invalid_tokens(self) -> None:
        parsed = DorotheumScraper._parse_cookie_header(
            "cf_clearance=abc123; malformed; __cf_bm=token; empty="
        )

        self.assertEqual(parsed, {"cf_clearance": "abc123", "__cf_bm": "token"})

    def test_cookie_header_falls_back_to_env(self) -> None:
        with patch.dict("os.environ", {"DOROTHEUM_COOKIE_HEADER": "cf_clearance=env"}, clear=False):
            scraper = self._build_scraper()

        self.assertEqual(scraper._cookie_header, "cf_clearance=env")

    def test_cookie_header_argument_takes_precedence_over_env(self) -> None:
        with patch.dict("os.environ", {"DOROTHEUM_COOKIE_HEADER": "cf_clearance=env"}, clear=False):
            scraper = self._build_scraper(cookie_header="cf_clearance=explicit")

        self.assertEqual(scraper._cookie_header, "cf_clearance=explicit")

    def test_fetch_html_uses_playwright(self) -> None:
        scraper = self._build_scraper()

        with patch.object(
            scraper,
            "fetch_html_playwright",
            return_value="<html>ok</html>",
        ) as pw_mock:
            html = scraper.fetch_html("https://www.dorotheum.com/de/co/gemaelde-1/")

        self.assertEqual(html, "<html>ok</html>")
        pw_mock.assert_called_once_with(
            "https://www.dorotheum.com/de/co/gemaelde-1/",
            max_retries=1,
        )


if __name__ == "__main__":
    unittest.main()
