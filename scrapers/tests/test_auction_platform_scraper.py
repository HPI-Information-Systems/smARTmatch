from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from PIL import Image

from scrapers.utils.auction_scraper import AuctionPlatformScraper
from scrapers.utils.image_storage import default_images_dir, safe_image_prefix
from scrapers.utils.scraper import _write_bytes_atomically


class _FakeDB:
    def __init__(self) -> None:
        self.session = object()
        self.commits = 0
        self.existing_artwork = None
        self.image_calls: list[dict[str, object]] = []

    def commit(self) -> None:
        self.commits += 1

    def get_or_create_auction_platform(self, *, name: str):
        return SimpleNamespace(auction_platform_id=f"platform:{name}")

    def find_auction_artwork_by_lot(
        self, lot_id=None, lot_url=None, auction_platform_id=None
    ):
        del lot_id, lot_url, auction_platform_id
        return self.existing_artwork

    def set_auction_artwork_images(self, **kwargs) -> None:
        self.image_calls.append(kwargs)


class _DummyAuctionScraper(AuctionPlatformScraper):
    def __init__(
        self,
        *,
        db: _FakeDB,
        urls: list[str],
        purge: bool = False,
        commit_every: int = 2,
        download_images: bool = False,
    ) -> None:
        super().__init__(
            db=db,
            platform_name="dummy",
            min_wait=0.1,
            max_wait=0.2,
            purge=purge,
            commit_every=commit_every,
            download_images=download_images,
            images_dir=None,
            module_file=__file__,
        )
        self.urls = urls
        self.processed: list[str] = []
        self.prepared = False
        self.after = False
        self.purged = False

    def get_urls(self, skip: int = 0):
        return self.urls[skip:]

    def scrape_url(self, url: str):
        self.processed.append(url)
        return None

    def _prepare_run(self) -> None:
        self.prepared = True

    def _after_run(self) -> None:
        self.after = True

    def purge_existing_data(self) -> None:
        self.purged = True


def _png_bytes(*, width: int, height: int) -> bytes:
    image = Image.new("RGBA", (width, height), (10, 20, 30, 120))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class AuctionPlatformScraperTests(unittest.TestCase):
    def test_precommit_fence_prevents_data_commit_after_lock_loss(self) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(db=db, urls=[])
        scraper.on_before_batch_commit = lambda: (_ for _ in ()).throw(
            RuntimeError("lock lost")
        )

        with self.assertRaisesRegex(RuntimeError, "lock lost"):
            scraper._commit_batch()

        self.assertEqual(db.commits, 0)

    def test_run_batches_commits_and_updates_stats(self) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(
            db=db, urls=["a", "b", "c", "d", "e"], commit_every=2
        )

        scraper.run(skip=1, report_every=10)

        self.assertEqual(scraper.processed, ["b", "c", "d", "e"])
        self.assertTrue(scraper.prepared)
        self.assertTrue(scraper.after)
        self.assertEqual(scraper.stats["urls_total"], 4)
        self.assertEqual(scraper.stats["urls_processed"], 4)
        # idx 2 + idx 4 + final commit at end
        self.assertEqual(db.commits, 3)

    def test_run_publishes_queue_progress_after_discovery_and_each_item(self) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(
            db=db,
            urls=["a", "b", "c"],
            commit_every=10,
        )
        snapshots: list[tuple[int, int]] = []
        scraper.on_progress = lambda: snapshots.append(
            (scraper.stats["urls_total"], scraper.stats["urls_processed"])
        )

        scraper.run(report_every=10)

        self.assertEqual(snapshots, [(3, 0), (3, 1), (3, 2), (3, 3)])

    def test_run_with_no_urls_still_calls_after_hook(self) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(db=db, urls=[])

        scraper.run()

        self.assertTrue(scraper.prepared)
        self.assertTrue(scraper.after)
        self.assertEqual(db.commits, 0)

    def test_after_hook_runs_when_scraping_fails(self) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(db=db, urls=["bad"])
        scraper.scrape_url = lambda _url: (_ for _ in ()).throw(RuntimeError("boom"))

        with self.assertRaisesRegex(RuntimeError, "boom"):
            scraper.run()

        self.assertTrue(scraper.after)

    def test_purge_is_triggered_when_enabled(self) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(db=db, urls=[], purge=True)

        scraper.run()

        self.assertTrue(scraper.purged)

    def test_fetch_html_uses_shared_request_function(self) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(db=db, urls=[])

        with patch(
            "scrapers.utils.auction_scraper.request_html", return_value="<html></html>"
        ) as request_mock:
            html = scraper.fetch_html("https://example.org")

        self.assertEqual(html, "<html></html>")
        request_mock.assert_called_once_with(
            "https://example.org",
            min_wait=0.1,
            max_wait=0.2,
            log=scraper.log,
        )

    def test_download_lot_images_respects_toggle(self) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(db=db, urls=[], download_images=False)

        with patch.object(
            scraper, "download_images", side_effect=RuntimeError("should not be called")
        ):
            local_paths = scraper.download_lot_images(
                ["https://example.org/a.jpg"], lot_id="1"
            )

        self.assertEqual(local_paths, [])
        self.assertFalse(scraper.last_image_download_complete)

    def test_empty_image_list_is_authoritative_and_resets_source_state(self) -> None:
        scraper = _DummyAuctionScraper(db=_FakeDB(), urls=[], download_images=True)
        scraper.last_downloaded_image_sources = {"stale.jpg": "https://old.test"}
        scraper.last_image_download_complete = False

        local_paths = scraper.download_lot_images([], lot_id="1")

        self.assertEqual(local_paths, [])
        self.assertEqual(scraper.last_downloaded_image_sources, {})
        self.assertTrue(scraper.last_image_download_complete)

    def test_download_lot_images_handles_download_errors(self) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(db=db, urls=[], download_images=True)

        with patch.object(
            scraper,
            "download_images",
            side_effect=OSError("No space left on device"),
        ):
            local_paths = scraper.download_lot_images(
                ["https://example.org/a.jpg"], lot_id="1"
            )

        self.assertEqual(local_paths, [])
        self.assertFalse(scraper.last_image_download_complete)

    def test_default_images_dir_is_shared_db_images(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            images_dir = default_images_dir()

        self.assertEqual(images_dir.name, "images")
        self.assertEqual(images_dir.parent.name, "db")

    def test_default_images_dir_can_be_overridden_by_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ, {"SMARTMATCH_IMAGES_DIR": temp_dir}, clear=True
            ):
                self.assertEqual(default_images_dir(), Path(temp_dir).resolve())

    def test_download_lot_images_uses_platform_code_and_db_uuid_prefix(self) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(db=db, urls=[], download_images=True)
        artwork_id = UUID("11111111-1111-4111-8111-111111111111")

        with (
            patch.object(
                scraper, "resolve_storage_artwork_id", return_value=artwork_id
            ),
            patch.object(
                scraper, "download_images", return_value=["saved.jpg"]
            ) as mocked,
        ):
            local_paths = scraper.download_lot_images(
                ["https://example.org/a.jpg"], lot_id="Lot / 1"
            )

        self.assertEqual(local_paths, ["saved.jpg"])
        mocked.assert_called_once_with(
            ["https://example.org/a.jpg"],
            scraper.images_dir,
            safe_image_prefix(scraper.image_prefix, artwork_id),
            include_url_hash=True,
        )

    def test_resolve_storage_artwork_id_prefers_existing_db_row_id(self) -> None:
        db = _FakeDB()
        db.existing_artwork = SimpleNamespace(
            auction_artwork_id=UUID("22222222-2222-4222-8222-222222222222")
        )
        scraper = _DummyAuctionScraper(db=db, urls=[], download_images=True)

        resolved = scraper.resolve_storage_artwork_id(
            lot_id="LOT-1",
            lot_url="https://example.org/lot-1",
        )

        self.assertEqual(str(resolved), "22222222-2222-4222-8222-222222222222")

    def test_changed_image_url_does_not_reuse_stale_indexed_file(self) -> None:
        scraper = _DummyAuctionScraper(db=_FakeDB(), urls=[], download_images=True)
        artwork_id = UUID("44444444-4444-4444-8444-444444444444")
        with tempfile.TemporaryDirectory() as tmp:
            scraper.images_dir = Path(tmp)
            with patch(
                "scrapers.utils.scraper.request_image",
                return_value=_png_bytes(width=100, height=100),
            ) as request:
                first = scraper.download_lot_images(
                    ["https://example.org/first.jpg"],
                    lot_id="LOT-1",
                    artwork_id=artwork_id,
                )
                second = scraper.download_lot_images(
                    ["https://example.org/replacement.jpg"],
                    lot_id="LOT-1",
                    artwork_id=artwork_id,
                )

        self.assertNotEqual(first, second)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            scraper.last_downloaded_image_sources,
            {second[0]: "https://example.org/replacement.jpg"},
        )

    def test_failed_hashed_download_makes_image_reconcile_non_authoritative(
        self,
    ) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(db=db, urls=[], download_images=True)
        artwork_id = UUID("55555555-5555-4555-8555-555555555555")

        with tempfile.TemporaryDirectory() as tmp:
            scraper.images_dir = Path(tmp)
            legacy_path = scraper.images_dir / (
                f"{safe_image_prefix(scraper.image_prefix, artwork_id)}_0.jpg"
            )
            legacy_path.write_bytes(b"valid legacy image")
            with patch("scrapers.utils.scraper.request_image", return_value=None):
                paths = scraper.download_lot_images(
                    ["https://example.org/replacement.jpg"],
                    lot_id="LOT-1",
                    artwork_id=artwork_id,
                )

            self.assertEqual(paths, [])
            self.assertTrue(legacy_path.exists())
            self.assertFalse(scraper.last_image_download_complete)
            self.assertEqual(scraper.last_downloaded_image_sources, {})
            scraper.set_lot_images(artwork_id=artwork_id, image_paths=paths)

        self.assertEqual(
            db.image_calls,
            [
                {
                    "auction_artwork_id": artwork_id,
                    "image_paths": [],
                    "image_source_urls": {},
                    "authoritative": False,
                }
            ],
        )

    def test_partial_hashed_download_preserves_success_and_is_non_authoritative(
        self,
    ) -> None:
        scraper = _DummyAuctionScraper(db=_FakeDB(), urls=[], download_images=True)
        artwork_id = UUID("66666666-6666-4666-8666-666666666666")
        first_url = "https://example.org/first.jpg"

        with tempfile.TemporaryDirectory() as tmp:
            scraper.images_dir = Path(tmp)
            with patch(
                "scrapers.utils.scraper.request_image",
                side_effect=[
                    _png_bytes(width=100, height=100),
                    RuntimeError("unexpected provider failure"),
                ],
            ):
                paths = scraper.download_lot_images(
                    [first_url, "https://example.org/failed.jpg"],
                    lot_id="LOT-1",
                    artwork_id=artwork_id,
                )

        self.assertEqual(len(paths), 1)
        self.assertFalse(scraper.last_image_download_complete)
        self.assertEqual(scraper.last_downloaded_image_sources, {paths[0]: first_url})

    def test_invalid_hashed_cache_is_replaced_before_becoming_authoritative(
        self,
    ) -> None:
        scraper = _DummyAuctionScraper(db=_FakeDB(), urls=[], download_images=True)
        artwork_id = UUID("77777777-7777-4777-8777-777777777777")
        image_url = "https://example.org/image.jpg"

        with tempfile.TemporaryDirectory() as tmp:
            scraper.images_dir = Path(tmp)
            prefix = safe_image_prefix(scraper.image_prefix, artwork_id)
            url_hash = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:10]
            cached_path = scraper.images_dir / f"{prefix}_0_{url_hash}.jpg"
            cached_path.write_bytes(b"partial corrupt file")

            with patch(
                "scrapers.utils.scraper.request_image",
                return_value=_png_bytes(width=100, height=100),
            ) as request:
                paths = scraper.download_lot_images(
                    [image_url], lot_id="LOT-1", artwork_id=artwork_id
                )

            request.assert_called_once()
            self.assertEqual(paths, [str(cached_path)])
            self.assertTrue(scraper.last_image_download_complete)
            with Image.open(cached_path) as saved:
                self.assertEqual(saved.format, "JPEG")
                saved.verify()

            with patch(
                "scrapers.utils.scraper.request_image",
                side_effect=AssertionError("valid cache should be reused"),
            ):
                cached_paths = scraper.download_lot_images(
                    [image_url], lot_id="LOT-1", artwork_id=artwork_id
                )
            self.assertEqual(cached_paths, paths)
            self.assertTrue(scraper.last_image_download_complete)

    def test_atomic_image_write_removes_temporary_file_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "image.jpg"
            with patch.object(Path, "replace", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(OSError, "disk error"):
                    _write_bytes_atomically(destination, b"jpeg bytes")

            self.assertEqual(list(Path(tmp).iterdir()), [])
            self.assertFalse(destination.exists())

    def test_download_lot_images_uses_url_hash_to_prevent_stale_cache_aliases(
        self,
    ) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(db=db, urls=[], download_images=True)
        artwork_id = UUID("33333333-3333-4333-8333-333333333333")

        with tempfile.TemporaryDirectory() as tmp:
            scraper.images_dir = Path(tmp)
            with patch(
                "scrapers.utils.scraper.request_image",
                return_value=_png_bytes(width=4000, height=1000),
            ):
                paths = scraper.download_lot_images(
                    ["https://example.org/a.jpg"],
                    lot_id="LOT-1",
                    artwork_id=artwork_id,
                )

            self.assertEqual(len(paths), 1)
            self.assertTrue(scraper.last_image_download_complete)
            self.assertEqual(
                scraper.last_downloaded_image_sources,
                {paths[0]: "https://example.org/a.jpg"},
            )
            saved_path = Path(paths[0])
            url_hash = hashlib.sha1(b"https://example.org/a.jpg").hexdigest()[:10]
            self.assertEqual(
                saved_path.name,
                f"{safe_image_prefix(scraper.image_prefix, artwork_id)}_0_{url_hash}.jpg",
            )
            with Image.open(saved_path) as saved:
                self.assertEqual(saved.format, "JPEG")
                self.assertEqual(saved.size, (2000, 500))

    def test_download_lot_images_does_not_upscale_small_images(self) -> None:
        db = _FakeDB()
        scraper = _DummyAuctionScraper(db=db, urls=[], download_images=True)
        artwork_id = UUID("44444444-4444-4444-8444-444444444444")

        with tempfile.TemporaryDirectory() as tmp:
            scraper.images_dir = Path(tmp)
            with patch(
                "scrapers.utils.scraper.request_image",
                return_value=_png_bytes(width=1200, height=800),
            ):
                paths = scraper.download_lot_images(
                    ["https://example.org/b.jpg"],
                    lot_id="LOT-2",
                    artwork_id=artwork_id,
                )

            self.assertEqual(len(paths), 1)
            with Image.open(Path(paths[0])) as saved:
                self.assertEqual(saved.format, "JPEG")
                self.assertEqual(saved.size, (1200, 800))


if __name__ == "__main__":
    unittest.main()
