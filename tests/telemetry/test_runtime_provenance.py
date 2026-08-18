"""Focused offline telemetry tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from telemetry import provenance as telemetry


class RuntimeReproducibilityTests(unittest.TestCase):
    def test_requirement_lock_parser_reads_exact_pins_without_installing_them(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock = Path(temp_dir) / "requirements.txt"
            lock.write_text(
                "# generated lock\n"
                "Example_Package[extra]==1.2.3\n"
                "other-package>=4\n"
            )
            metadata = telemetry._requirement_lock_metadata(lock)

        self.assertTrue(metadata["available"])
        self.assertEqual(metadata["package_count"], 1)
        self.assertEqual(metadata["packages"], {"example-package": "1.2.3"})

    def test_runtime_metadata_uses_component_requirement_locks(self) -> None:
        metadata = telemetry._runtime_reproducibility_metadata()

        self.assertEqual(metadata["packages"]["torch"], "2.9.1")
        self.assertEqual(
            metadata["requirement_locks"]["application"]["packages"]["psycopg"],
            "3.2.2",
        )
        self.assertEqual(
            metadata["requirement_locks"]["matching_pipeline"]["packages"]["vllm"],
            "0.14.1",
        )
