import os
import unittest
from unittest.mock import patch

from frontend import app


class DatabaseSessionLifecycleTests(unittest.TestCase):
    def tearDown(self):
        app.session.remove()

    def test_app_contexts_get_distinct_database_sessions(self):
        with app.app.app_context():
            first_session = app.session()
            self.assertTrue(app.session.registry.has())

        self.assertFalse(app.session.registry.has())

        with app.app.app_context():
            second_session = app.session()

        self.assertIsNot(first_session, second_session)
        self.assertFalse(app.session.registry.has())

    def test_teardown_rolls_back_closes_and_removes_the_session(self):
        db_session = app.session()
        with (
            patch.object(db_session, "rollback") as rollback,
            patch.object(db_session, "close") as close,
        ):
            app._remove_db_session()

        rollback.assert_called_once_with()
        close.assert_called_once_with()
        self.assertFalse(app.session.registry.has())


class FrontendServerTests(unittest.TestCase):
    def test_main_serves_the_app_with_waitress(self):
        with (
            patch.dict(
                os.environ,
                {"FRONTEND_HOST": "127.0.0.1", "FRONTEND_PORT": "8080"},
                clear=False,
            ),
            patch("waitress.serve") as serve,
            patch.object(app.app, "run") as flask_run,
        ):
            app.main()

        serve.assert_called_once_with(app.app, host="127.0.0.1", port=8080)
        flask_run.assert_not_called()


class SourceUrlTests(unittest.TestCase):
    def test_safe_source_url_accepts_only_absolute_http_urls(self):
        self.assertEqual(
            app.safe_source_url(" https://example.test/artwork "),
            "https://example.test/artwork",
        )
        self.assertEqual(
            app.safe_source_url("HTTP://example.test/lot"),
            "HTTP://example.test/lot",
        )

        for value in (
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "//example.test/artwork",
            "/relative/path",
            "https:///missing-host",
            "",
            None,
        ):
            with self.subTest(value=value):
                self.assertIsNone(app.safe_source_url(value))


if __name__ == "__main__":
    unittest.main()
