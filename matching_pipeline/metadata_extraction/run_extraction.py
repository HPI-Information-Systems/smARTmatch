"""
Extraction entry point: fetches unprocessed descriptions from the DB,
then runs the Qwen LLM extractor over them.
"""

import logging
from pathlib import Path

from matching_pipeline.shared.env import get_model_config
from matching_pipeline.metadata_extraction.get_descriptions import get_descriptions
from matching_pipeline.metadata_extraction.qwen_extract_information import extract_metadata

logger = logging.getLogger(__name__)


def run_extraction(descriptions_file: Path, backend: str | None = None) -> None:
    logger.info("Fetching unprocessed descriptions from DB...")
    get_descriptions(output_file=descriptions_file)

    logger.info("Extracting metadata...")
    config = get_model_config()
    extract_metadata(
        descriptions_file=descriptions_file,
        backend=config.backend if backend is None else backend,
    )


if __name__ == "__main__":
    _ROOT = Path(__file__).parent.parent
    run_extraction(descriptions_file=_ROOT / "descriptions.jsonl")
