import re
import unittest
from unittest.mock import patch

from frontend import app


class HomeTemplateTests(unittest.TestCase):
    def setUp(self):
        app.app.config["TESTING"] = True
        self.client = app.app.test_client()

    def render_home(self, match_count):
        with (
            patch.object(app, "get_match_count", return_value=match_count),
            patch.object(app, "get_top_unlabeled_auction_previews", return_value=[]),
        ):
            return self.client.get("/")

    def test_review_button_is_enabled_when_one_match_remains(self):
        response = self.render_home(1)

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertRegex(
            html,
            re.compile(r'<a class="btn btn-primary"\s+role="button"\s+href="/match">'),
        )

    def test_review_button_is_disabled_when_no_matches_remain(self):
        response = self.render_home(0)

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<span class="btn btn-primary disabled" role="button">', html)
        self.assertNotRegex(
            html,
            re.compile(r'<a class="btn btn-primary"\s+role="button"\s+href="/match">'),
        )


if __name__ == "__main__":
    unittest.main()
