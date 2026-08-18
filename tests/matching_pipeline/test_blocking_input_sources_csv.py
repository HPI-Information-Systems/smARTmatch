"""Offline unit coverage for blocking CSV input helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from matching_pipeline.image_blocking import input_sources
from matching_pipeline.image_blocking.input_sources import ImageFileRow


class InputSourceCsvTests(unittest.TestCase):
    def test_row_and_csv_round_trip_for_both_path_styles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            csv_dir = root / "snapshots"
            inside = csv_dir / "images" / "lost.jpg"
            outside = root / "auction.jpg"
            inside.parent.mkdir(parents=True)
            inside.write_bytes(b"lost")
            outside.write_bytes(b"auction")
            lost = ImageFileRow(
                " lost-id ",
                inside,
                content_version=2,
                content_sha256="a" * 64,
            )
            auction = ImageFileRow(
                "auction-id",
                outside,
                content_version=3,
                content_sha256="b" * 64,
            )

            self.assertEqual(lost.as_strings(), (" lost-id ", str(inside)))
            written = input_sources.write_image_file_csv(
                csv_dir / "inputs.csv", [lost], [auction]
            )
            text = written.read_text(encoding="utf-8")
            self.assertIn("images/lost.jpg", text)
            self.assertIn("../auction.jpg", text)

            lost_rows, auction_rows = input_sources.read_image_file_csv(written)
            self.assertEqual(
                lost_rows,
                [
                    ImageFileRow(
                        "lost-id",
                        inside,
                        content_version=2,
                        content_sha256="a" * 64,
                    )
                ],
            )
            self.assertEqual(auction_rows, [auction])

    def test_read_csv_file_and_header_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.csv"
            with self.assertRaisesRegex(FileNotFoundError, "Image input CSV not found"):
                input_sources.read_image_file_csv(missing)

            bad_header = root / "bad.csv"
            bad_header.write_text("file_id,role\n1,lost\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing columns: file_path"):
                input_sources.read_image_file_csv(bad_header)

            with self.assertRaisesRegex(ValueError, "file_id, file_path, role"):
                input_sources._require_columns(None)

    def test_read_csv_rejects_bad_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "image.jpg"
            image.write_bytes(b"x")

            cases = (
                ("1,image.jpg,other\n", ValueError, "Invalid role"),
                (",image.jpg,lost\n", ValueError, "Missing file_id"),
                ("1,,lost\n", ValueError, "Missing file_path"),
                (
                    "1,missing.jpg,lost\n",
                    FileNotFoundError,
                    "file_id=1.*path=.*missing.jpg",
                ),
                (
                    "1,image.jpg,lost\n1,image.jpg,lost\n",
                    ValueError,
                    "Duplicate lost file_id",
                ),
            )
            for body, exception, message in cases:
                with self.subTest(message=message):
                    path = root / "case.csv"
                    path.write_text(
                        "file_id,file_path,role\n" + body, encoding="utf-8"
                    )
                    with self.assertRaisesRegex(exception, message):
                        input_sources.read_image_file_csv(path)

    def test_small_helpers(self) -> None:
        self.assertEqual(input_sources._clean(None), "")
        self.assertEqual(input_sources._clean("  value "), "value")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertEqual(
                input_sources._resolve_csv_path(root, "image.jpg"),
                root / "image.jpg",
            )
            absolute = root / "absolute.jpg"
            self.assertEqual(
                input_sources._resolve_csv_path(root / "other", str(absolute)),
                absolute,
            )


if __name__ == "__main__":
    unittest.main()
