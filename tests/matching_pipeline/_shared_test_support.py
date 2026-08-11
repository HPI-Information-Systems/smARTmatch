"""Temporary filesystem and environment support for shared-module tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq

from matching_pipeline.shared import env


class TemporaryPipelineTest(unittest.TestCase):
    """Provide isolated cache, image, and environment locations."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.image_root = self.root / "images"
        self.cache_root = self.root / "cache"
        self.image_root.mkdir()
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "CACHE_DIR": str(self.cache_root),
                "SMARTMATCH_IMAGES_DIR": str(self.image_root),
            },
            clear=True,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def ranking_dir(self) -> Path:
        path = env.env_auction_to_lost_rankings_dir()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_raw_ranking(self, name: str = "part-000000.parquet", **columns) -> Path:
        path = self.ranking_dir() / name
        pq.write_table(pa.table(columns), path)
        return path
