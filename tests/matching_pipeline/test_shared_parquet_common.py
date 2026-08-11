"""Offline unit tests for shared Parquet IO helpers."""

from __future__ import annotations

import builtins
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq

from matching_pipeline.shared.artifacts import parquet_common
from tests.matching_pipeline._shared_test_support import TemporaryPipelineTest


class ParquetCommonTests(TemporaryPipelineTest):
    def test_require_pyarrow_success_and_import_error(self) -> None:
        loaded_pa, loaded_pq = parquet_common.require_pyarrow()
        self.assertIs(loaded_pa, pa)
        self.assertIs(loaded_pq, pq)
        real_import = builtins.__import__

        def reject_pyarrow(name, *args, **kwargs):
            if name == "pyarrow":
                raise ImportError("offline test")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=reject_pyarrow):
            with self.assertRaisesRegex(RuntimeError, "require pyarrow") as raised:
                parquet_common.require_pyarrow()
        self.assertIsInstance(raised.exception.__cause__, ImportError)

    def test_atomic_write_success_and_failure_cleanup(self) -> None:
        output = self.root / "nested" / "artifact.parquet"

        class Writer:
            @staticmethod
            def write_table(table, path) -> None:
                Path(path).write_text(str(table), encoding="utf-8")

        parquet_common.write_table_atomic(Writer, "new", output)
        self.assertEqual(output.read_text(encoding="utf-8"), "new")
        self.assertEqual(list(output.parent.glob(".*.tmp.*")), [])

        output.write_text("old", encoding="utf-8")

        class FailingWriter:
            @staticmethod
            def write_table(_table, path) -> None:
                Path(path).write_text("partial", encoding="utf-8")
                raise OSError("write failed")

        with self.assertRaisesRegex(OSError, "write failed"):
            parquet_common.write_table_atomic(FailingWriter, "new", output)
        self.assertEqual(output.read_text(encoding="utf-8"), "old")
        self.assertEqual(list(output.parent.glob(".*.tmp.*")), [])
