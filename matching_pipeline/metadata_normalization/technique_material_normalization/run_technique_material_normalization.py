"""Normalize auction_artwork material/technique into dict arrays.

Input/output is the descriptions JSONL file used by the rest of normalization.
For each auction record this stage adds:
    - dict_material_name
    - dict_technique_name

The DB is used only as a reference source for material_variant/technique_variant.
No auction_artwork rows are updated here directly; write_descriptions.py persists
both raw fields and normalized dict arrays in one UPDATE.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from matching_pipeline.shared.db import connect as db_connect
from matching_pipeline.metadata_normalization.technique_material_normalization.dict_lookup import (
    load_dict_context,
    normalize_existing_array,
    resolve_material_dict_names,
    resolve_technique_dict_names,
)

logger = logging.getLogger(__name__)


def normalize_record(
    record: dict[str, Any],
    context,
    *,
    overwrite: bool = True,
) -> bool:
    """Mutate one JSONL record. Return True if dict fields changed."""
    changed = False

    if overwrite or not normalize_existing_array(record.get("dict_material_name")):
        new_material_dict = resolve_material_dict_names(record.get("material"), context)
        if normalize_existing_array(record.get("dict_material_name")) != new_material_dict:
            record["dict_material_name"] = new_material_dict
            changed = True

    if overwrite or not normalize_existing_array(record.get("dict_technique_name")):
        new_technique_dict = resolve_technique_dict_names(record.get("technique"), context)
        if normalize_existing_array(record.get("dict_technique_name")) != new_technique_dict:
            record["dict_technique_name"] = new_technique_dict
            changed = True

    return changed


def run_technique_material_normalization(descriptions_file: Path) -> None:
    if not descriptions_file.exists():
        raise FileNotFoundError(f"Descriptions file not found: {descriptions_file}")

    with db_connect() as conn:
        context = load_dict_context(conn)

    total_records = 0
    changed_records = 0

    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(descriptions_file.parent),
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

        with descriptions_file.open("r", encoding="utf-8") as src:
            for line in src:
                stripped = line.strip()
                if not stripped:
                    continue

                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    # Preserve malformed lines instead of silently dropping input.
                    tmp.write(line)
                    continue

                total_records += 1
                if normalize_record(record, context, overwrite=True):
                    changed_records += 1

                tmp.write(json.dumps(record, ensure_ascii=False) + "\n")

    tmp_path.replace(descriptions_file)
    logger.info(
        "Technique/material normalization: "
        f"updated {changed_records}/{total_records} auction records in {descriptions_file}"
    )


if __name__ == "__main__":
    _ROOT = Path(__file__).resolve().parents[2]
    run_technique_material_normalization(_ROOT / "descriptions.jsonl")
