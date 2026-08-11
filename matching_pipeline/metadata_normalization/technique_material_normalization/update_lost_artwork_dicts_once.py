"""One-time normalization of lost_artwork material/technique dict arrays.

Run this after setup_once() has seeded dict_material, dict_technique,
material_variant, and technique_variant. The function is idempotent: by default
it fills only empty dict_material_name/dict_technique_name fields and does not
overwrite curated dictionary values already present in lost_artwork.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from matching_pipeline.shared.db import connect as db_connect
from matching_pipeline.metadata_normalization.technique_material_normalization.dict_lookup import (
    load_dict_context,
    normalize_existing_array,
    resolve_material_dict_names,
    resolve_technique_dict_names,
)

logger = logging.getLogger(__name__)


def _is_empty_array(value: Any) -> bool:
    return len(normalize_existing_array(value)) == 0


def _needs_update(
    current_material_dict: Any,
    current_technique_dict: Any,
    overwrite: bool,
) -> bool:
    if overwrite:
        return True
    return _is_empty_array(current_material_dict) or _is_empty_array(current_technique_dict)


def update_lost_artwork_dicts_once(
    *,
    batch_size: int = 1000,
    overwrite: bool = False,
) -> dict[str, int]:
    """Fill lost_artwork.dict_material_name and dict_technique_name once.

    Args:
        batch_size: Number of UPDATE rows sent in one executemany call.
        overwrite: If False, only empty dict arrays are filled. If True, dict
            arrays are recalculated from raw material/technique values.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")

    rows_scanned = 0
    rows_updated = 0
    update_buffer: list[tuple[list[str], list[str], str]] = []

    with db_connect() as conn:
        context = load_dict_context(conn)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    lost_artwork_id,
                    material,
                    technique,
                    dict_material_name,
                    dict_technique_name
                FROM lost_artwork
                WHERE %s
                   OR COALESCE(cardinality(dict_material_name), 0) = 0
                   OR COALESCE(cardinality(dict_technique_name), 0) = 0
                ORDER BY lost_artwork_id
                """,
                (overwrite,),
            )
            rows = cur.fetchall()

        with conn.cursor() as cur:
            for (
                lost_artwork_id,
                material,
                technique,
                current_material_dict,
                current_technique_dict,
            ) in rows:
                rows_scanned += 1

                if not _needs_update(current_material_dict, current_technique_dict, overwrite):
                    continue

                current_material = normalize_existing_array(current_material_dict)
                current_technique = normalize_existing_array(current_technique_dict)

                new_material = (
                    resolve_material_dict_names(material, context)
                    if overwrite or not current_material
                    else current_material
                )
                new_technique = (
                    resolve_technique_dict_names(technique, context)
                    if overwrite or not current_technique
                    else current_technique
                )

                if new_material == current_material and new_technique == current_technique:
                    continue

                update_buffer.append((new_material, new_technique, str(lost_artwork_id)))

                if len(update_buffer) >= batch_size:
                    cur.executemany(
                        """
                        UPDATE lost_artwork
                        SET dict_material_name = %s,
                            dict_technique_name = %s
                        WHERE lost_artwork_id = %s
                        """,
                        update_buffer,
                    )
                    rows_updated += len(update_buffer)
                    update_buffer.clear()

            if update_buffer:
                cur.executemany(
                    """
                    UPDATE lost_artwork
                    SET dict_material_name = %s,
                        dict_technique_name = %s
                    WHERE lost_artwork_id = %s
                    """,
                    update_buffer,
                )
                rows_updated += len(update_buffer)
                update_buffer.clear()

        conn.commit()

    result = {"rows_scanned": rows_scanned, "rows_updated": rows_updated}
    logger.info(
        "lost_artwork technique/material dict update: "
        f"updated {rows_updated}/{rows_scanned} rows"
    )
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill lost_artwork dict_material_name/dict_technique_name from raw material/technique."
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Recalculate existing non-empty dict arrays instead of filling only empty ones.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    update_lost_artwork_dicts_once(batch_size=args.batch_size, overwrite=args.overwrite)


if __name__ == "__main__":
    main()
