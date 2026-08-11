from __future__ import annotations

import unittest
from unittest.mock import call, patch

from scrapers.lottissimo.scraper import LottissimoScraper


class _FakeDB:
    def __init__(self) -> None:
        self.session = object()


class LottissimoHttpTests(unittest.TestCase):
    def _build_scraper(self, **kwargs) -> LottissimoScraper:
        return LottissimoScraper(
            db=_FakeDB(),
            min_wait=0.0,
            max_wait=0.0,
            **kwargs,
        )

    def test_fetch_html_delegates_to_playwright(self) -> None:
        scraper = self._build_scraper()

        with patch.object(
            scraper,
            "fetch_html_playwright",
            return_value="<html>lots</html>",
        ) as pw_mock:
            result = scraper.fetch_html("https://www.lot-tissimo.com/de/")

        self.assertEqual(result, "<html>lots</html>")
        pw_mock.assert_called_once_with(
            "https://www.lot-tissimo.com/de/",
            wait_for_selector=None,
        )

    def test_fetch_html_passes_wait_for_selector(self) -> None:
        scraper = self._build_scraper()
        selector = 'a[href*="/lot-"]'

        with patch.object(
            scraper,
            "fetch_html_playwright",
            return_value="<html>lots</html>",
        ) as pw_mock:
            result = scraper.fetch_html("https://www.lot-tissimo.com/de/", wait_for_selector=selector)

        self.assertEqual(result, "<html>lots</html>")
        pw_mock.assert_called_once_with(
            "https://www.lot-tissimo.com/de/",
            wait_for_selector=selector,
        )

    def test_prepare_run_starts_browser(self) -> None:
        scraper = self._build_scraper()

        with patch.object(scraper, "_start_browser") as mock_start:
            scraper._prepare_run()

        mock_start.assert_called_once()

    def test_after_run_stops_browser(self) -> None:
        scraper = self._build_scraper()

        with patch.object(scraper, "_stop_browser") as mock_stop:
            scraper._after_run()

        mock_stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
