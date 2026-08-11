from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scrapers.runtime_config import (
    load_request_cooldown_override,
    save_request_cooldown_override,
)


class RuntimeConfigTests(unittest.TestCase):
    def test_override_is_shared_through_atomic_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runtime.json"
            with patch.dict(
                "os.environ",
                {"SCRAPER_RUNTIME_CONFIG_PATH": str(path)},
                clear=False,
            ):
                self.assertIsNone(load_request_cooldown_override())
                save_request_cooldown_override(12.5)
                self.assertEqual(load_request_cooldown_override(), 12.5)

    def test_invalid_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runtime.json"
            path.write_text("not-json", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {"SCRAPER_RUNTIME_CONFIG_PATH": str(path)},
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "Invalid scraper runtime config"):
                    load_request_cooldown_override()


if __name__ == "__main__":
    unittest.main()
