"""
Top-level normalization pipeline: runs artist, dating, dimension, and
technique/material normalization in sequence on descriptions_file, then
writes the results to the DB.
"""

import logging
from pathlib import Path

from matching_pipeline.shared.env import get_model_config
from matching_pipeline.metadata_normalization.artist_normalization.run_artist_normalization import (
    run_artist_normalization,
)
from matching_pipeline.metadata_normalization.dating_normalization.run_dating_normalization import (
    run_dating_normalization,
)
from matching_pipeline.metadata_normalization.dimension_normalization.run_dimension_normalization import (
    run_dimension_normalization,
)

from matching_pipeline.metadata_normalization.technique_material_normalization.run_technique_material_normalization import (
    run_technique_material_normalization,
)

from matching_pipeline.metadata_normalization.write_descriptions import write_descriptions

logger = logging.getLogger(__name__)


def run_normalization(
    descriptions_file: Path,
    unmatched_file: Path,
    backend: str | None = None,
) -> None:
    config = get_model_config()
    selected_backend = config.backend if backend is None else backend

    logger.info("=== Artist normalization ===")
    run_artist_normalization(descriptions_file=descriptions_file)

    logger.info("=== Dating normalization ===")
    run_dating_normalization(
        descriptions_file=descriptions_file,
        unmatched_file=unmatched_file,
        backend=selected_backend,
    )

    logger.info("=== Dimension normalization ===")
    run_dimension_normalization(
        descriptions_file=descriptions_file, backend=selected_backend
    )

    logger.info("=== Technique/material normalization ===")
    run_technique_material_normalization(descriptions_file=descriptions_file)

    logger.info("=== Writing results to DB ===")
    write_descriptions(descriptions_file=descriptions_file)


if __name__ == "__main__":
    _ROOT = Path(__file__).parent.parent
    run_normalization(
        descriptions_file=_ROOT / "descriptions.jsonl",
        unmatched_file=_ROOT / "descriptions_dating_unmatched.jsonl",
    )
