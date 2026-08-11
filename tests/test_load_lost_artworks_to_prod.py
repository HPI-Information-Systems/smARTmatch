"""Tests for lost-artwork import path preservation helpers."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_fake_psycopg() -> None:
    psycopg_mod = types.ModuleType("psycopg")
    psycopg_mod.Connection = object
    psycopg_mod.Cursor = object
    psycopg_mod.connect = lambda *args, **kwargs: None

    sql_mod = types.ModuleType("psycopg.sql")
    sql_mod.Composable = object
    sql_mod.SQL = lambda value="": value
    sql_mod.Identifier = lambda value: value
    sql_mod.Placeholder = lambda: "%s"
    psycopg_mod.sql = sql_mod

    rows_mod = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = object()

    types_mod = types.ModuleType("psycopg.types")
    json_mod = types.ModuleType("psycopg.types.json")

    class Jsonb:
        def __init__(self, value):
            self.value = value

    json_mod.Jsonb = Jsonb
    types_mod.json = json_mod

    sys.modules.setdefault("psycopg", psycopg_mod)
    sys.modules.setdefault("psycopg.sql", sql_mod)
    sys.modules.setdefault("psycopg.rows", rows_mod)
    sys.modules.setdefault("psycopg.types", types_mod)
    sys.modules.setdefault("psycopg.types.json", json_mod)


def _load_module():
    _install_fake_psycopg()
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "load_lost_artworks_to_prod.py"
    spec = importlib.util.spec_from_file_location("load_lost_artworks_to_prod", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


class LoadLostArtworksToProdPathTests(unittest.TestCase):
    def test_source_image_ref_preserves_original_filename_case(self) -> None:
        image = MODULE.source_image_ref("/srv/images/FancyPainting.JPG")

        self.assertEqual(image.file_name, "FancyPainting")
        self.assertEqual(image.file_extension, "jpg")
        self.assertEqual(image.target_file_name, "FancyPainting.JPG")

    def test_source_image_ref_keeps_extensionless_filenames(self) -> None:
        image = MODULE.source_image_ref("/srv/images/extensionless-image")

        self.assertEqual(image.file_name, "extensionless-image")
        self.assertIsNone(image.file_extension)
        self.assertEqual(image.target_file_name, "extensionless-image")

    def test_image_root_relative_path_uses_original_filename(self) -> None:
        image = MODULE.source_image_ref("/srv/images/LA453288_0.jpg")

        self.assertEqual(
            MODULE.image_root_relative_path(Path("db/images"), image.target_file_name),
            "db/images/LA453288_0.jpg",
        )

    def test_validate_target_paths_rejects_conflicting_flattened_filenames(self) -> None:
        lost_rows = [
            {"lost_artwork_id": "lost-1", "img_paths": ["/a/dup.jpg"]},
            {"lost_artwork_id": "lost-2", "img_paths": ["/b/dup.jpg"]},
        ]

        with self.assertRaisesRegex(RuntimeError, "conflicting target filenames"):
            MODULE.validate_target_paths(Path("db/images"), lost_rows)


if __name__ == "__main__":
    unittest.main()
