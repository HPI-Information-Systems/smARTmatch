from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from scraper_dashboard.storage_stats import (
    _load_auction_image_paths,
    _load_lost_art_image_paths,
    build_storage_stats,
    collect_image_file_stats,
    human_readable_bytes,
    load_scraper_image_paths,
)


class StorageStatsTests(unittest.TestCase):
    def test_collect_image_file_stats_counts_unique_existing_image_files_only(self) -> None:
        with TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            images_dir = repo_root / "db" / "data-production" / "images"
            images_dir.mkdir(parents=True)

            rel_image = images_dir / "lot_1.jpg"
            rel_image.write_bytes(b"a" * 5)

            abs_image = images_dir / "lot_2.PNG"
            abs_image.write_bytes(b"b" * 7)

            note = images_dir / "note.txt"
            note.write_text("not an image", encoding="utf-8")

            image_count, disk_bytes = collect_image_file_stats(
                repo_root=repo_root,
                image_paths=[
                    str(rel_image.relative_to(repo_root)),
                    str(abs_image),
                    str(rel_image.relative_to(repo_root)),  # duplicate DB reference
                    str(note.relative_to(repo_root)),
                    "db/data-production/images/missing.jpg",
                ],
            )

        self.assertEqual(image_count, 2)
        self.assertEqual(disk_bytes, 12)

    def test_collect_image_file_stats_resolves_legacy_paths_relative_to_dashboard_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "scraper_dashboard").mkdir()
            image_file = repo_root / "db" / "data-production" / "images" / "legacy.jpg"
            image_file.parent.mkdir(parents=True)
            image_file.write_bytes(b"x" * 9)

            image_count, disk_bytes = collect_image_file_stats(
                repo_root=repo_root,
                image_paths=["../db/data-production/images/legacy.jpg"],
            )

        self.assertEqual((image_count, disk_bytes), (1, 9))

    def test_collect_image_file_stats_returns_zero_when_no_paths_exist(self) -> None:
        with TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            image_count, disk_bytes = collect_image_file_stats(
                repo_root=repo_root,
                image_paths=["db/data-production/images/missing.jpg"],
            )

        self.assertEqual((image_count, disk_bytes), (0, 0))

    def test_human_readable_bytes(self) -> None:
        self.assertEqual(human_readable_bytes(0), "0 B")
        self.assertEqual(human_readable_bytes(1023), "1023 B")
        self.assertEqual(human_readable_bytes(1024), "1.0 KB")
        self.assertEqual(human_readable_bytes(1536), "1.5 KB")
        self.assertEqual(human_readable_bytes(2 * 1024 * 1024), "2.0 MB")

    def test_load_scraper_image_paths_uses_auction_source_for_auction_scrapers(self) -> None:
        db = MagicMock()
        session = MagicMock()
        db.SessionLocal.return_value = session

        with patch(
            "scraper_dashboard.storage_stats._load_auction_image_paths",
            return_value=iter(["a.jpg", "b.jpg"]),
        ) as load_auction:
            paths = load_scraper_image_paths(
                db=db,
                scraper_info={"table": "auction", "platform_name": "Christie's"},
            )

        self.assertEqual(paths, ["a.jpg", "b.jpg"])
        load_auction.assert_called_once_with(session=session, platform_name="Christie's")
        session.close.assert_called_once()

    def test_load_auction_image_paths_prefers_image_file_links(self) -> None:
        session = MagicMock()
        session.execute.return_value.scalars.return_value = ["img/a.jpg", "img/b.jpg"]

        def fake_has_columns(_session, table_name, _required):
            return table_name in {"auction_artwork_image_file", "image_file"}

        with patch(
            "scraper_dashboard.storage_stats._table_has_columns",
            side_effect=fake_has_columns,
        ):
            paths = list(
                _load_auction_image_paths(session=session, platform_name="Christie's")
            )

        self.assertEqual(paths, ["img/a.jpg", "img/b.jpg"])

    def test_load_lost_image_paths_prefers_image_file_links(self) -> None:
        session = MagicMock()
        session.execute.return_value.scalars.return_value = ["lost/1.jpg"]

        def fake_has_columns(_session, table_name, _required):
            return table_name in {"lost_artwork_image_file", "image_file"}

        with patch(
            "scraper_dashboard.storage_stats._table_has_columns",
            side_effect=fake_has_columns,
        ):
            paths = list(_load_lost_art_image_paths(session=session))

        self.assertEqual(paths, ["lost/1.jpg"])

    def test_load_scraper_image_paths_uses_lost_table_for_lost_scraper(self) -> None:
        db = MagicMock()
        session = MagicMock()
        db.SessionLocal.return_value = session

        with patch(
            "scraper_dashboard.storage_stats._load_lost_art_image_paths",
            return_value=iter(["lost.jpg"]),
        ) as load_lost:
            paths = load_scraper_image_paths(
                db=db,
                scraper_info={"table": "lost"},
            )

        self.assertEqual(paths, ["lost.jpg"])
        load_lost.assert_called_once_with(session=session)
        session.close.assert_called_once()

    def test_build_storage_stats_returns_serializable_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            image_file = repo_root / "db" / "data-production" / "images" / "one.jpeg"
            image_file.parent.mkdir(parents=True)
            image_file.write_bytes(b"1" * 10)

            with patch(
                "scraper_dashboard.storage_stats.load_scraper_image_paths",
                return_value=[str(image_file.relative_to(repo_root))],
            ):
                payload = build_storage_stats(
                    db=MagicMock(),
                    repo_root=repo_root,
                    scraper_name="demo",
                    scraper_info={"table": "auction", "platform_name": "Demo"},
                )

        self.assertEqual(payload["image_file_count"], 1)
        self.assertEqual(payload["image_disk_bytes"], 10)
        self.assertEqual(payload["image_disk_usage_human"], "10 B")

    def test_build_storage_stats_returns_zero_payload_on_query_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch(
                "scraper_dashboard.storage_stats.load_scraper_image_paths",
                side_effect=RuntimeError("boom"),
            ):
                payload = build_storage_stats(
                    db=MagicMock(),
                    repo_root=repo_root,
                    scraper_name="demo",
                    scraper_info={"table": "auction", "platform_name": "Demo"},
                )

        self.assertEqual(payload["image_file_count"], 0)
        self.assertEqual(payload["image_disk_bytes"], 0)
        self.assertEqual(payload["image_disk_usage_human"], "0 B")


if __name__ == "__main__":
    unittest.main()
