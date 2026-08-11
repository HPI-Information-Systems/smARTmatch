import logging
from pathlib import Path

from matching_pipeline.shared.env import get_model_config
from matching_pipeline.metadata_normalization.dimension_normalization.qwen_extract_dimensions import (
    normalize_with_qwen,
)

logger = logging.getLogger(__name__)


def run_dimension_normalization(
    descriptions_file: Path,
    backend: str | None = None,
) -> None:
    logger.info("Dimension normalization: Qwen...")
    config = get_model_config()
    normalize_with_qwen(
        descriptions_file=descriptions_file,
        backend=config.backend if backend is None else backend,
    )


if __name__ == "__main__":
    _ROOT = Path(__file__).parent.parent.parent
    run_dimension_normalization(descriptions_file=_ROOT / "descriptions.jsonl")
