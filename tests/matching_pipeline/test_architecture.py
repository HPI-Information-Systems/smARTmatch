"""Static deployment-boundary checks for matching_pipeline."""

from __future__ import annotations

import ast
import hashlib
import os
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPO_ROOT / "matching_pipeline"
_CLASSIFIER_SHA256 = "220247dc5d19b952959367de9de1b228b3bba5b38e4cbc758a477988fee6eea7"


class ImagePipelineArchitectureTests(unittest.TestCase):
    def test_shared_layer_does_not_import_blocking(self) -> None:
        imports = _package_imports(_PACKAGE_ROOT / "shared")
        blocking = sorted(
            name
            for name in imports
            if name == "matching_pipeline.image_blocking" or name.startswith("matching_pipeline.image_blocking.")
        )
        self.assertEqual(blocking, [])

    def test_excluded_runtime_modules_are_absent(self) -> None:
        for relative_path in (
            "blocking_experiments",
            "datasets",
            "keypoint",
            "labeling",
            "image_blocking/image_paths.py",
            "image_blocking/retrieve.py",
        ):
            self.assertFalse((_PACKAGE_ROOT / relative_path).exists(), relative_path)

    def test_classifier_is_copied_byte_for_byte(self) -> None:
        classifier = _PACKAGE_ROOT / "image_matching" / "classifier.pkl"
        digest = hashlib.sha256(classifier.read_bytes()).hexdigest()
        self.assertEqual(digest, _CLASSIFIER_SHA256)

    def test_requirements_pin_model_compatibility(self) -> None:
        requirements = (_PACKAGE_ROOT / "requirements.txt").read_text()
        self.assertIn("scikit-learn==1.9.0", requirements)
        dockerfile = (_PACKAGE_ROOT / "Dockerfile").read_text()
        self.assertIn("LightGlue@eb42fee2d71449efb0aa5c10549752b5d75384d8", dockerfile)

    def test_db_config_requires_credentials(self) -> None:
        from matching_pipeline.shared.db import connect_db

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "POSTGRES_DB"):
                connect_db()

    def test_db_config_passes_validated_tcp_settings(self) -> None:
        from matching_pipeline.shared import db

        env = {
            "POSTGRES_DB": "smartmatch",
            "POSTGRES_USER": "runner",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_HOST": "db",
            "POSTGRES_PORT": "5432",
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            db.psycopg, "connect", return_value="connection"
        ) as connect:
            self.assertEqual(db.connect_db(), "connection")
        connect.assert_called_once_with(
            dbname="smartmatch",
            user="runner",
            password="secret",
            host="db",
            port=5432,
        )


def _package_imports(root: Path) -> set[str]:
    imports: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
    return imports


if __name__ == "__main__":
    unittest.main()
