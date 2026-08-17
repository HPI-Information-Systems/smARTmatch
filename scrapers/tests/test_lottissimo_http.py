from __future__ import annotations

import unittest
from unittest import mock
from unittest.mock import patch
from uuid import UUID

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

    def test_clear_title_scopes_update_to_artwork_uuid(self) -> None:
        db = mock.Mock()
        session = db._get_session.return_value
        scraper = LottissimoScraper(
            db=db,
            min_wait=0.0,
            max_wait=0.0,
        )
        artwork_id = UUID("33333333-3333-4333-8333-333333333333")

        scraper._clear_title_column(artwork_id=artwork_id)

        statement, params = session.execute.call_args.args
        statement_text = str(statement)
        self.assertIn("where auction_artwork_id = :artwork_id", statement_text)
        self.assertNotIn("where lot_id", statement_text)
        self.assertEqual(params, {"artwork_id": artwork_id})

    def test_browser_fallback_uses_same_validated_user_agents(self) -> None:
        scraper = self._build_scraper()

        profile_user_agents = tuple(
            headers["User-Agent"] for _session, headers in scraper._http_profiles
        )
        self.assertEqual(scraper._browser_user_agents, profile_user_agents)

    def test_fetch_html_prefers_server_rendered_response(self) -> None:
        scraper = self._build_scraper()

        listing_html = (
            '<html><a href="/de-de/auction-catalogues/house/'
            'catalogue-id-sale/lot-abc">lot</a>' + (" " * 5_000) + "</html>"
        )
        with (
            patch(
                "scrapers.lottissimo.scraper.request_html",
                return_value=listing_html,
            ) as http_mock,
            patch.object(scraper, "fetch_html_playwright") as pw_mock,
        ):
            result = scraper.fetch_html("https://www.lot-tissimo.com/de/")

        self.assertIn("lot-abc", result)
        http_mock.assert_called_once()
        pw_mock.assert_not_called()

    def test_fetch_html_rotates_profile_before_browser_fallback(self) -> None:
        scraper = self._build_scraper()
        listing_html = (
            '<html><a href="/de-de/auction-catalogues/house/'
            'catalogue-id-sale/lot-abc">lot</a>' + (" " * 5_000) + "</html>"
        )

        with (
            patch(
                "scrapers.lottissimo.scraper.request_html",
                side_effect=[None, listing_html],
            ) as http_mock,
            patch.object(scraper, "fetch_html_playwright") as pw_mock,
        ):
            result = scraper.fetch_html(
                "https://www.lot-tissimo.com/de/",
                wait_for_selector='a[href*="/lot-"]',
            )

        self.assertIn("lot-abc", result)
        self.assertEqual(http_mock.call_count, 2)
        self.assertEqual(scraper._active_http_profile, 2)
        pw_mock.assert_not_called()

    def test_fetch_html_falls_back_when_expected_listing_content_is_absent(
        self,
    ) -> None:
        scraper = self._build_scraper()
        selector = 'a[href*="/lot-"]'

        with (
            patch(
                "scrapers.lottissimo.scraper.request_html",
                return_value="<html><title>Consent</title>" + (" " * 5_000) + "</html>",
            ),
            patch.object(
                scraper,
                "fetch_html_playwright",
                return_value="<html>lots</html>",
            ) as pw_mock,
        ):
            result = scraper.fetch_html(
                "https://www.lot-tissimo.com/de/",
                wait_for_selector=selector,
            )

        self.assertEqual(result, "<html>lots</html>")
        pw_mock.assert_called_once_with(
            "https://www.lot-tissimo.com/de/",
            wait_for_selector=selector,
        )

    def test_fetch_html_falls_back_to_playwright_for_waf_response(self) -> None:
        scraper = self._build_scraper()
        selector = 'a[href*="/lot-"]'

        with (
            patch(
                "scrapers.lottissimo.scraper.request_html",
                return_value="<html>AwsWafIntegration</html>",
            ),
            patch.object(
                scraper,
                "fetch_html_playwright",
                return_value="<html>lots</html>",
            ) as pw_mock,
        ):
            result = scraper.fetch_html(
                "https://www.lot-tissimo.com/de/",
                wait_for_selector=selector,
            )

        self.assertEqual(result, "<html>lots</html>")
        pw_mock.assert_called_once_with(
            "https://www.lot-tissimo.com/de/",
            wait_for_selector=selector,
        )

    def test_fetch_html_rejects_malformed_lot_detail(self) -> None:
        scraper = self._build_scraper()
        lot_url = (
            "https://www.lot-tissimo.com/de-de/auction-catalogues/house/"
            "catalogue-id-sale/lot-abc"
        )

        with (
            patch(
                "scrapers.lottissimo.scraper.request_html",
                return_value="<html><title>Lot</title>" + (" " * 5_000) + "</html>",
            ),
            patch.object(
                scraper,
                "fetch_html_playwright",
                return_value="<html>browser detail</html>",
            ) as pw_mock,
        ):
            result = scraper.fetch_html(lot_url)

        self.assertEqual(result, "<html>browser detail</html>")
        pw_mock.assert_called_once_with(lot_url, wait_for_selector=None)

    def test_prepare_run_does_not_start_browser_eagerly(self) -> None:
        scraper = self._build_scraper()

        with patch.object(scraper, "_start_browser") as mock_start:
            scraper._prepare_run()

        mock_start.assert_not_called()

    def test_after_run_stops_browser(self) -> None:
        scraper = self._build_scraper()

        with patch.object(scraper, "_stop_browser") as mock_stop:
            scraper._after_run()

        mock_stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
