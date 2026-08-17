from __future__ import annotations

import unittest
from unittest import mock
from unittest.mock import patch
from uuid import UUID

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

    def test_clear_missing_identity_scopes_update_to_artwork_uuid(self) -> None:
        db = mock.Mock()
        db._get_table_columns.return_value = {
            "title",
            "artist_id",
            "artist_full_name",
            "artist_raw_data",
        }
        session = db._get_session.return_value
        scraper = DorotheumScraper(
            db=db,
            min_wait=0.0,
            max_wait=0.0,
            request_max_retries=1,
        )
        artwork_id = UUID("22222222-2222-4222-8222-222222222222")

        scraper._clear_missing_identity_columns(
            artwork_id=artwork_id,
            clear_title=True,
            clear_artist=True,
        )

        statement, params = session.execute.call_args.args
        statement_text = str(statement)
        self.assertIn("where auction_artwork_id = :artwork_id", statement_text)
        self.assertNotIn("where lot_id", statement_text)
        self.assertEqual(params, {"artwork_id": artwork_id})

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
