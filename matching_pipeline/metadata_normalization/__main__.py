"""Run metadata normalization for the extraction-stage handoff files."""

from pathlib import Path

from matching_pipeline.metadata_normalization.run_normalization import run_normalization
from matching_pipeline.shared.logging_setup import configure_logging

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    configure_logging()
    run_normalization(
        descriptions_file=_PIPELINE_ROOT / "metadata_extraction" / "descriptions.jsonl",
        unmatched_file=(
            _PIPELINE_ROOT
            / "metadata_extraction"
            / "descriptions_dating_unmatched.jsonl"
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
