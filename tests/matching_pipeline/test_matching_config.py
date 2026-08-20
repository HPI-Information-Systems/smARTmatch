from __future__ import annotations

import os
import unittest
from unittest import mock

from matching_pipeline.image_matching import config


class MatchingConfigTests(unittest.TestCase):
    def test_resize_workers_are_positive_and_configurable(self) -> None:
        with mock.patch.dict(
            os.environ,
            {config.MATCHING_IMAGE_RESIZE_WORKERS_ENV: "3"},
            clear=False,
        ):
            self.assertEqual(config.matching_image_resize_workers_from_env(), 3)

        with mock.patch.dict(
            os.environ,
            {config.MATCHING_IMAGE_RESIZE_WORKERS_ENV: "0"},
            clear=False,
        ), self.assertRaisesRegex(ValueError, "positive integer"):
            config.matching_image_resize_workers_from_env()


if __name__ == "__main__":
    unittest.main()
