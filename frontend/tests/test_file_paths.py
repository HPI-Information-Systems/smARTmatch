import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.exceptions import NotFound

from frontend import app


class DbFilePathSecurityTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.image_root = self.root / "db" / "images"
        self.cache_root = self.root / "cache"
        self.image_root.mkdir(parents=True)
        self.cache_root.mkdir()

        previous_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous_cwd)

        environment = patch.dict(
            os.environ,
            {
                "SMARTMATCH_IMAGES_DIR": str(self.image_root),
                "CACHE_DIR": str(self.cache_root),
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def _create_file(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test file")
        return path

    def test_allows_files_under_image_and_cache_roots(self):
        image = self._create_file(self.image_root / "nested" / "image.jpg")
        plot = self._create_file(self.cache_root / "plots" / "keypoints.png")

        self.assertEqual(app._existing_file_path(image), str(image.resolve()))
        self.assertEqual(app._existing_file_path(plot), str(plot.resolve()))

    def test_allows_supported_relative_database_paths(self):
        image = self._create_file(self.image_root / "image.jpg")
        extensionless = self._create_file(
            self.image_root / "spsg" / "8262f768ca77afc3990dac4aa5d9dc1d6ae8d916"
        )
        plot = self._create_file(self.cache_root / "plot.png")

        self.assertEqual(
            app._existing_file_path("db/images/image.jpg"), str(image.resolve())
        )
        self.assertEqual(
            app._existing_file_path(
                "spsg/8262f768ca77afc3990dac4aa5d9dc1d6ae8d916"
            ),
            str(extensionless.resolve()),
        )
        self.assertEqual(app._existing_file_path("plot.png"), str(plot.resolve()))

    def test_rejects_existing_absolute_path_outside_approved_roots(self):
        outside = self._create_file(self.root / "private.txt")

        with self.assertRaises(NotFound):
            app._existing_file_path(outside)

    def test_rejects_relative_path_that_escapes_an_approved_root(self):
        outside = self._create_file(self.image_root.parent / "private.txt")

        with self.assertRaises(NotFound):
            app._existing_file_path("../private.txt")

    def test_rejects_symlink_to_file_outside_approved_roots(self):
        outside = self._create_file(self.root / "private.txt")
        link = self.image_root / "linked.txt"
        link.symlink_to(outside)

        with self.assertRaises(NotFound):
            app._existing_file_path(link)


if __name__ == "__main__":
    unittest.main()
