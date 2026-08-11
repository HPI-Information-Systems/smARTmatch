"""
Two-stage dating normalization: regex first, then Qwen on the leftovers
regex couldn't match (written by normalize_with_regex to unmatched_file).
"""

import logging
from pathlib import Path

from matching_pipeline.shared.env import get_model_config
from matching_pipeline.metadata_normalization.dating_normalization.qwen_classify_mentions import (
    normalize_with_qwen,
)
from matching_pipeline.metadata_normalization.dating_normalization.regex_filter import normalize_with_regex

logger = logging.getLogger(__name__)


def run_dating_normalization(
    descriptions_file: Path,
    unmatched_file: Path,
    backend: str | None = None,
) -> None:
    logger.info("Dating normalization: regex...")
    normalize_with_regex(
        descriptions_file=descriptions_file, unmatched_file=unmatched_file
    )

    logger.info("Dating normalization: Qwen (unmatched only)...")
    config = get_model_config()
    normalize_with_qwen(
        descriptions_file=descriptions_file,
        unmatched_file=unmatched_file,
        backend=config.backend if backend is None else backend,
    )


if __name__ == "__main__":
    _ROOT = Path(__file__).parent.parent.parent
    run_dating_normalization(
        descriptions_file=_ROOT / "descriptions.jsonl",
        unmatched_file=_ROOT / "descriptions_dating_unmatched.jsonl",
    )
