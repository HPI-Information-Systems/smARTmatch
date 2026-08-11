"""Offline unit tests for shared image-file artifacts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

from matching_pipeline.shared import env
from matching_pipeline.shared.artifacts import image_files
from tests.matching_pipeline._shared_test_support import TemporaryPipelineTest


class ImageFileArtifactTests(TemporaryPipelineTest):
    def test_roundtrip_mapping_and_object_rows(self) -> None:
        object_row = SimpleNamespace(
            file_id=" object-id ", file_path=self.image_root / "nested" / "one.jpg"
        )
        output = image_files.write_image_files_parquet(
            "auction",
            [
                {"file_id": 7, "file_path": "relative.jpg"},
                object_row,
            ],
        )
        table = pq.read_table(output)
        self.assertEqual(table.schema.names, ["file_id", "file_path"])
        self.assertEqual(table.column("file_id").to_pylist(), ["7", "object-id"])
        self.assertEqual(
            table.column("file_path").to_pylist(),
            ["relative.jpg", "nested/one.jpg"],
        )
        self.assertEqual(
            image_files.read_image_files_parquet("auction"),
            {
                "7": str((self.image_root / "relative.jpg").resolve()),
                "object-id": str((self.image_root / "nested/one.jpg").resolve()),
            },
        )

    def test_missing_artifact_and_row_text_validation(self) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "artifact not found"):
            image_files.read_image_files_parquet("lost")

        invalid_rows = [
            ({"file_path": "a.jpg"}, "Missing file_id"),
            ({"file_id": " ", "file_path": "a.jpg"}, "Empty file_id"),
            ({"file_id": "a"}, "Missing file_path"),
            ({"file_id": "a", "file_path": "  "}, "Empty file_path"),
            (SimpleNamespace(), "Missing file_id"),
        ]
        for row, message in invalid_rows:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    image_files._coerce_image_rows([row])

    def test_write_rejects_duplicate_and_outside_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate image file_id at row 2"):
            image_files._coerce_image_rows(
                [
                    {"file_id": "same", "file_path": "one.jpg"},
                    {"file_id": "same", "file_path": "two.jpg"},
                ]
            )
        outside = self.root / "outside.jpg"
        for path in (outside, Path("..") / "outside.jpg"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "outside image root"):
                    image_files._coerce_image_rows(
                        [{"file_id": "id", "file_path": path}]
                    )

    def test_read_rejects_schema_duplicate_and_unsafe_paths(self) -> None:
        path = env.env_image_files_parquet_path("auction")
        path.parent.mkdir(parents=True)
        pq.write_table(pa.table({"file_id": ["id"]}), path)
        with self.assertRaises((pa.ArrowInvalid, pa.ArrowNotImplementedError)):
            image_files.read_image_files_parquet("auction")

        invalid_tables = [
            ({"file_id": [None], "file_path": ["a.jpg"]}, "Missing file_id"),
            ({"file_id": [" "], "file_path": ["a.jpg"]}, "Empty file_id"),
            ({"file_id": ["id"], "file_path": [None]}, "Missing file_path"),
            ({"file_id": ["id"], "file_path": [" "]}, "Empty file_path"),
            (
                {"file_id": ["id"], "file_path": [str(self.image_root / "a.jpg")]},
                "must be relative",
            ),
            (
                {"file_id": ["id"], "file_path": ["../escape.jpg"]},
                "escapes image root",
            ),
            (
                {"file_id": ["same", "same"], "file_path": ["a.jpg", "b.jpg"]},
                "Duplicate image file_id",
            ),
        ]
        for columns, message in invalid_tables:
            with self.subTest(message=message):
                pq.write_table(pa.table(columns), path)
                with self.assertRaisesRegex(ValueError, message):
                    image_files.read_image_files_parquet("auction")
