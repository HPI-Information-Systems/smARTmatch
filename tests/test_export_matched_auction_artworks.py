"""Tests for the matched auction-artwork transfer shell script."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_matched_auction_artworks.sh"
TRACKED_IMAGE = "db/images/pipeline_test_set/Q151047_historic.jpg"
BEGIN_MARKER = "__SMARTMATCH_IMAGE_PATHS_HEX_BEGIN__"
END_MARKER = "__SMARTMATCH_IMAGE_PATHS_HEX_END__"


class ExportMatchedAuctionArtworksTests(unittest.TestCase):
    def test_shell_syntax_and_help(self) -> None:
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True, cwd=ROOT)
        result = subprocess.run(
            [str(SCRIPT), "--help"],
            check=True,
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertIn("transfer/auction_artworks.sql", result.stdout)
        self.assertIn("transfer/rsync-files.txt", result.stdout)
        self.assertNotIn("DB_MODE", result.stdout)
        self.assertNotIn("PSQL_BIN", result.stdout)

    def test_fake_export_publishes_sql_and_repo_relative_image_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_docker = self._fake_docker(temp, TRACKED_IMAGE)
            transfer_dir = temp / "transfer"

            result = self._run_export(fake_docker, transfer_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (transfer_dir / "auction_artworks.sql").read_text(),
                "BEGIN;\nCOMMIT;\n",
            )
            self.assertEqual(
                (transfer_dir / "rsync-files.txt").read_text(),
                f"{TRACKED_IMAGE}\n",
            )

    def test_missing_image_does_not_replace_existing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_docker = self._fake_docker(temp, "db/images/not-present.jpg")
            transfer_dir = temp / "transfer"
            transfer_dir.mkdir()
            sql_path = transfer_dir / "auction_artworks.sql"
            rsync_path = transfer_dir / "rsync-files.txt"
            sql_path.write_text("old sql\n")
            rsync_path.write_text("old manifest\n")

            result = self._run_export(fake_docker, transfer_dir)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Missing 1 selected image file", result.stderr)
            self.assertEqual(sql_path.read_text(), "old sql\n")
            self.assertEqual(rsync_path.read_text(), "old manifest\n")

    def test_source_selects_matches_without_exporting_match_rows(self) -> None:
        source = SCRIPT.read_text()
        self.assertIn("SELECT DISTINCT ms.auction_id", source)
        self.assertIn("FROM match_score ms", source)
        self.assertNotIn("COPY _transfer_match_score", source)
        self.assertNotIn("INSERT INTO match_score", source)
        self.assertNotIn("DELETE FROM match_score", source)
        self.assertNotIn("DB_MODE", source)
        self.assertNotIn("PSQL_BIN", source)
        self.assertIn('docker compose exec -T "$DB_SERVICE"', source)
        self.assertIn("false, NULL::timestamptz", source)
        self.assertNotIn("FORMAT csv", source)
        self.assertIn(
            "INSERT INTO material_variant SELECT * FROM _transfer_material_variant\n"
            "-- Variants may already exist under a different UUID but the same natural key.\n"
            "ON CONFLICT DO NOTHING;",
            source,
        )
        self.assertIn(
            "INSERT INTO technique_variant SELECT * FROM _transfer_technique_variant\n"
            "-- Variants may already exist under a different UUID but the same natural key.\n"
            "ON CONFLICT DO NOTHING;",
            source,
        )

    def test_import_skips_existing_artworks_without_linking_their_images(self) -> None:
        source = SCRIPT.read_text()
        self.assertNotIn(
            "Target already contains a selected auction artwork",
            source,
        )
        self.assertIn(
            "WHERE target.auction_artwork_id = source.auction_artwork_id",
            source,
        )
        self.assertIn(
            "JOIN _transfer_auction_id_map auction_map\n"
            "  ON auction_map.source_id = source.auction_artwork_id",
            source,
        )
        self.assertIn(
            "JOIN _transfer_auction_id_map auction_map\n"
            "  ON auction_map.source_id = link.auction_artwork_id",
            source,
        )

    def test_source_transaction_supports_temp_tables_and_rolls_back(self) -> None:
        source = SCRIPT.read_text()
        self.assertIn(
            "BEGIN ISOLATION LEVEL REPEATABLE READ, READ WRITE;",
            source,
        )
        self.assertNotIn(
            "BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY;",
            source,
        )
        self.assertIn("\nROLLBACK;\nSOURCE_SQL", source)

    def test_export_fragments_do_not_add_blank_copy_rows(self) -> None:
        source = SCRIPT.read_text()
        self.assertIn("if ! run_psql \\\n    -R '' \\", source)
        self.assertIn(
            "SELECT E'__SMARTMATCH_IMAGE_PATHS_HEX_BEGIN__\\n';",
            source,
        )
        self.assertIn(
            "SELECT E'__SMARTMATCH_IMAGE_PATHS_HEX_END__\\n';",
            source,
        )

    def _run_export(
        self, fake_docker: Path, transfer_dir: Path
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "DB_SERVICE": "test-db",
                "PATH": f"{fake_docker.parent}{os.pathsep}{env.get('PATH', '')}",
                "TRANSFER_DIR": str(transfer_dir),
            }
        )
        return subprocess.run(
            [str(SCRIPT)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )

    @staticmethod
    def _fake_docker(temp: Path, image_path: str) -> Path:
        fake = temp / "docker"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "[[ \"$1\" == compose && \"$2\" == exec ]] || exit 2\n"
            "printf '%s\\n' 'BEGIN;' 'COMMIT;' "
            f"'{BEGIN_MARKER}' '{image_path.encode().hex()}' '{END_MARKER}'\n"
        )
        fake.chmod(0o755)
        return fake


if __name__ == "__main__":
    unittest.main()
