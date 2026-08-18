"""Focused offline telemetry tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telemetry import config as telemetry
from telemetry import database, delivery


class TelemetrySettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.images = self.root / "images"
        self.images.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {"SMARTMATCH_IMAGES_DIR": str(self.images)},
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_telemetry_enabled_rejects_malformed_boolean(self) -> None:
        os.environ["TELEMETRY_ENABLED"] = "treu"
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            telemetry._telemetry_enabled()
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            telemetry.load_telemetry_settings()

    def test_disabled_telemetry_does_not_require_an_endpoint(self) -> None:
        self.assertIsNone(telemetry.load_telemetry_settings())
        with mock.patch.object(database, "connect") as connect:
            self.assertEqual(delivery.try_send_daily_telemetry(), "disabled")
        connect.assert_not_called()

    def test_enabled_settings_require_https_and_accept_timeout(self) -> None:
        os.environ["TELEMETRY_ENABLED"] = "true"
        os.environ["TELEMETRY_ENDPOINT"] = "http://example.test/collect"
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            telemetry.load_telemetry_settings()

        os.environ["TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP"] = "true"
        with self.assertRaisesRegex(ValueError, "local hosts"):
            telemetry.load_telemetry_settings()

        os.environ["TELEMETRY_ENDPOINT"] = "http://telemetry_receiver:8080/collect"
        os.environ["TELEMETRY_AUTH_TOKEN"] = "unit-test-static-bearer-token"
        settings = telemetry.load_telemetry_settings()
        assert settings is not None
        self.assertEqual(settings.endpoint, os.environ["TELEMETRY_ENDPOINT"])
        self.assertEqual(settings.page_delay_min_seconds, 0.25)
        self.assertEqual(settings.page_delay_max_seconds, 0.5)

        os.environ["TELEMETRY_ENDPOINT"] = "https://telemetry_receiver/collect"
        os.environ["TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP"] = "false"
        with self.assertRaisesRegex(ValueError, "smartmatch.leogruetzner.com"):
            telemetry.load_telemetry_settings()

        os.environ["TELEMETRY_ENDPOINT"] = (
            "https://smartmatch.leogruetzner.com/api/telemetry"
        )
        os.environ["TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP"] = "false"
        os.environ["TELEMETRY_AUTH_TOKEN"] = "unit-test-static-bearer-token"
        os.environ["TELEMETRY_TIMEOUT_SECONDS"] = "2.5"
        settings = telemetry.load_telemetry_settings()
        assert settings is not None
        self.assertEqual(settings.timeout_seconds, 2.5)

        os.environ["TELEMETRY_ENDPOINT"] = "https://example.test/collect"
        with mock.patch.object(
            telemetry, "_is_local_telemetry_host", return_value=False
        ):
            with self.assertRaisesRegex(ValueError, "smartmatch.leogruetzner.com"):
                telemetry.load_telemetry_settings()

    def test_page_delay_range_is_configurable_and_validated(self) -> None:
        os.environ.update(
            {
                "TELEMETRY_ENABLED": "true",
                "TELEMETRY_ENDPOINT": "http://telemetry_receiver:8080/collect",
                "TELEMETRY_AUTH_TOKEN": "unit-test-static-bearer-token",
                "TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP": "true",
                "TELEMETRY_PAGE_DELAY_MIN_SECONDS": "0.1",
                "TELEMETRY_PAGE_DELAY_MAX_SECONDS": "0.3",
            }
        )
        settings = telemetry.load_telemetry_settings()
        assert settings is not None
        self.assertEqual(settings.page_delay_min_seconds, 0.1)
        self.assertEqual(settings.page_delay_max_seconds, 0.3)

        for minimum, maximum, message in (
            ("-0.1", "0.3", "MIN_SECONDS must be greater"),
            ("0.1", "not-a-number", "MAX_SECONDS must be a number"),
            ("0.4", "0.3", "MAX_SECONDS must be greater"),
        ):
            with self.subTest(minimum=minimum, maximum=maximum):
                os.environ["TELEMETRY_PAGE_DELAY_MIN_SECONDS"] = minimum
                os.environ["TELEMETRY_PAGE_DELAY_MAX_SECONDS"] = maximum
                with self.assertRaisesRegex(ValueError, message):
                    telemetry.load_telemetry_settings()
