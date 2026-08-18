"""Focused offline telemetry tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telemetry import summary
from telemetry import tree_hashing as telemetry


class DirectoryHashTests(unittest.TestCase):
    def test_tree_and_immediate_subdirectory_hashes_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "root.jpg").write_bytes(b"root")
            child = root / "scraper-a"
            nested = child / "nested"
            nested.mkdir(parents=True)
            (child / "a.jpg").write_bytes(b"a")
            (nested / "b.jpg").write_bytes(b"b")

            first = telemetry.hash_directory_tree(root)
            second = telemetry.hash_directory_tree(root)
            self.assertEqual(first.root, second.root)
            self.assertEqual(first.subdirectories, second.subdirectories)
            self.assertEqual(first.root.file_count, 3)
            self.assertEqual(set(first.subdirectories), {"scraper-a"})
            self.assertEqual(first.subdirectories["scraper-a"].file_count, 2)
            self.assertEqual(first.error_count, 0)
            self.assertEqual(first.hash_basis, "file_content")

            (nested / "b.jpg").write_bytes(b"changed")
            changed = telemetry.hash_directory_tree(root)
            self.assertNotEqual(changed.root.sha256, first.root.sha256)
            self.assertNotEqual(
                changed.subdirectories["scraper-a"].sha256,
                first.subdirectories["scraper-a"].sha256,
            )

    def test_path_size_mode_never_opens_image_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "large.jpg"
            with image.open("wb") as handle:
                handle.truncate(64 * 1024 * 1024)

            with mock.patch.object(
                Path,
                "open",
                side_effect=AssertionError("image content must not be opened"),
            ):
                snapshot = telemetry.hash_directory_tree(
                    root,
                    retain_file_paths={"large.jpg"},
                    hash_file_contents=False,
                )

        self.assertEqual(snapshot.hash_basis, "relative_path_and_size")
        self.assertEqual(snapshot.root.file_count, 1)
        self.assertEqual(snapshot.root.total_bytes, 64 * 1024 * 1024)
        self.assertEqual(set(snapshot.file_hashes), {"large.jpg"})
        payload = summary._tree_snapshot_payload(snapshot)
        self.assertEqual(payload["hash_basis"], "relative_path_and_size")
        self.assertEqual(payload["algorithm"], "sha256-merkle-path-size-tree-v1")

    def test_path_size_hash_ignores_content_but_tracks_path_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "image.jpg"
            image.write_bytes(b"first")
            first = telemetry.hash_directory_tree(root, hash_file_contents=False)

            image.write_bytes(b"other")
            same_size = telemetry.hash_directory_tree(root, hash_file_contents=False)
            self.assertEqual(same_size.root.sha256, first.root.sha256)

            image.write_bytes(b"different-size")
            changed_size = telemetry.hash_directory_tree(root, hash_file_contents=False)
            self.assertNotEqual(changed_size.root.sha256, first.root.sha256)

            renamed = root / "renamed.jpg"
            image.rename(renamed)
            changed_path = telemetry.hash_directory_tree(root, hash_file_contents=False)
            self.assertNotEqual(changed_path.root.sha256, changed_size.root.sha256)

    def test_ordering_spool_limit_marks_snapshot_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            telemetry, "_MAX_TREE_ORDERING_BYTES", 1
        ):
            root = Path(temp_dir)
            (root / "image.jpg").write_bytes(b"image")
            snapshot = telemetry.hash_directory_tree(root, hash_file_contents=False)
        self.assertGreater(snapshot.error_count, 0)
        self.assertEqual(snapshot.root.file_count, 0)

    def test_reported_root_subdirectories_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            telemetry, "_MAX_REPORTED_SUBDIRECTORIES", 2
        ):
            root = Path(temp_dir)
            for name in ("c", "a", "b"):
                (root / name).mkdir()
            snapshot = telemetry.hash_directory_tree(root, hash_file_contents=False)
        self.assertEqual(snapshot.subdirectory_count, 3)
        self.assertEqual(list(snapshot.subdirectories), ["a", "b"])
        self.assertTrue(snapshot.subdirectories_truncated)

    def test_tree_ordering_does_not_materialize_path_iterdir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("z.jpg", "a.jpg", "middle.jpg"):
                (root / name).write_bytes(name.encode())
            with mock.patch.object(
                Path,
                "iterdir",
                side_effect=AssertionError("unbounded iterdir must not be used"),
            ):
                snapshot = telemetry.hash_directory_tree(root, hash_file_contents=False)
        self.assertEqual(snapshot.root.file_count, 3)
