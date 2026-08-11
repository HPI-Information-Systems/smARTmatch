"""Read and write `image_files.parquet` artifact snapshots."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

from matching_pipeline.shared.env import env_image_files_parquet_path, env_image_root

from .parquet_common import require_pyarrow, write_table_atomic

_IMAGE_FILE_COLUMNS = ("file_id", "file_path")


class ImageFileRecord(Protocol):
    file_id: str
    file_path: str | Path


def write_image_files_parquet(
    role: str,
    rows: Iterable[ImageFileRecord | Mapping[str, object]],
) -> Path:
    """Write a role-specific `image_files.parquet` snapshot under `CACHE_DIR`."""
    output_path = env_image_files_parquet_path(role)
    file_ids, file_paths = _coerce_image_rows(rows)
    pa, pq = require_pyarrow()
    table = pa.table(
        {"file_id": file_ids, "file_path": file_paths},
        schema=pa.schema([("file_id", pa.string()), ("file_path", pa.string())]),
    )
    write_table_atomic(pq, table, output_path)
    return output_path


def read_image_files_parquet(role: str) -> dict[str, str]:
    """Read a role-specific `image_files.parquet` snapshot from `CACHE_DIR`."""
    input_path = env_image_files_parquet_path(role)
    if not input_path.is_file():
        raise FileNotFoundError(f"Image-file artifact not found: {input_path}")
    _pa, pq = require_pyarrow()
    table = pq.read_table(input_path, columns=list(_IMAGE_FILE_COLUMNS))
    return _image_table_to_path_map(table, input_path)


def _coerce_image_rows(
    rows: Iterable[ImageFileRecord | Mapping[str, object]],
) -> tuple[list[str], list[str]]:
    file_ids: list[str] = []
    file_paths: list[str] = []
    image_root = _image_root()
    seen: set[str] = set()
    for row_index, row in enumerate(rows, start=1):
        file_id, file_path = _image_row_values(row, row_index, image_root)
        if file_id in seen:
            raise ValueError(f"Duplicate image file_id at row {row_index}: {file_id}")
        seen.add(file_id)
        file_ids.append(file_id)
        file_paths.append(file_path)
    return file_ids, file_paths


def _image_row_values(
    row: ImageFileRecord | Mapping[str, object], row_index: int, image_root: Path
) -> tuple[str, str]:
    if isinstance(row, Mapping):
        raw_file_id = row.get("file_id")
        raw_file_path = row.get("file_path")
    else:
        raw_file_id = getattr(row, "file_id", None)
        raw_file_path = getattr(row, "file_path", None)
    file_id = _required_text(raw_file_id, "file_id", row_index).strip()
    file_path = _relative_image_path(raw_file_path, row_index, image_root)
    return file_id, file_path


def _image_table_to_path_map(table, input_path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    image_root = _image_root()
    file_ids = table.column("file_id").to_pylist()
    file_paths = table.column("file_path").to_pylist()
    for row_index, (raw_file_id, raw_file_path) in enumerate(
        zip(file_ids, file_paths), start=1
    ):
        file_id = _required_text(raw_file_id, "file_id", row_index).strip()
        file_path = _absolute_image_path(raw_file_path, row_index, image_root)
        if file_id in result:
            raise ValueError(f"Duplicate image file_id in {input_path}: {file_id}")
        result[file_id] = file_path
    return result


def _relative_image_path(value: object, row_index: int, image_root: Path) -> str:
    raw_path = Path(_required_text(value, "file_path", row_index)).expanduser()
    resolved = raw_path.resolve() if raw_path.is_absolute() else (image_root / raw_path).resolve()
    try:
        relative = resolved.relative_to(image_root)
    except ValueError as exc:
        raise ValueError(
            f"Image file_path at row {row_index} is outside image root {image_root}: {raw_path}"
        ) from exc
    return relative.as_posix()


def _absolute_image_path(value: object, row_index: int, image_root: Path) -> str:
    raw_path = Path(_required_text(value, "file_path", row_index)).expanduser()
    if raw_path.is_absolute():
        raise ValueError(
            f"Image file_path at row {row_index} must be relative to image root: {raw_path}"
        )
    resolved = (image_root / raw_path).resolve()
    try:
        resolved.relative_to(image_root)
    except ValueError as exc:
        raise ValueError(
            f"Image file_path at row {row_index} escapes image root: {raw_path}"
        ) from exc
    return str(resolved)


def _image_root() -> Path:
    return env_image_root()


def _required_text(value: object, field_name: str, row_index: int) -> str:
    if value is None:
        raise ValueError(f"Missing {field_name} at image row {row_index}")
    text = str(value)
    if not text.strip():
        raise ValueError(f"Empty {field_name} at image row {row_index}")
    return text
