"""Cross-process image-storage lock tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from shared.image_storage_lock import ImageStorageLockBusy, image_storage_lock


def test_multiple_writers_share_lock_but_cleanup_is_exclusive():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        with image_storage_lock(root, exclusive=False):
            with image_storage_lock(root, exclusive=False):
                with pytest.raises(ImageStorageLockBusy):
                    with image_storage_lock(root, exclusive=True, blocking=False):
                        raise AssertionError("exclusive lock should not be acquired")


def test_exclusive_cleanup_lock_blocks_new_writer():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        with image_storage_lock(root, exclusive=True):
            with pytest.raises(ImageStorageLockBusy):
                with image_storage_lock(root, exclusive=False, blocking=False):
                    raise AssertionError("writer lock should not be acquired")


def test_nested_writer_uses_configured_root_lock_inode():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        nested = root / "platform"
        with mock.patch.dict(
            "os.environ",
            {"SMARTMATCH_IMAGES_DIR": str(root)},
            clear=False,
        ):
            with image_storage_lock(nested, exclusive=False):
                with pytest.raises(ImageStorageLockBusy):
                    with image_storage_lock(root, exclusive=True, blocking=False):
                        raise AssertionError("nested writer must block root cleanup")
            assert (root / ".smartmatch-image-storage.lock").is_file()
            assert not (nested / ".smartmatch-image-storage.lock").exists()


def test_lock_file_is_created_inside_shared_image_root():
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        with image_storage_lock(root, exclusive=False):
            assert (root / ".smartmatch-image-storage.lock").is_file()
