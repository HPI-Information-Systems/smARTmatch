"""Shared helpers for SmartMatch Parquet artifact IO."""

from __future__ import annotations

import os
from pathlib import Path


def require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Parquet artifacts require pyarrow. Install pyarrow first.") from exc
    return pa, pq


def write_table_atomic(pq, table, path: Path) -> None:
    output_path = Path(path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f".{output_path.name}.tmp.{os.getpid()}")
    try:
        pq.write_table(table, tmp_path)
        os.replace(tmp_path, output_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
