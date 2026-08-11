"""
Shared stdout logging configuration for the metadata pipeline.

Every entrypoint/module in this pipeline should log through the standard
`logging` module (via `logging.getLogger(__name__)`) instead of `print()`, so
that all output shares one timestamp/level format regardless of which stage
produced it.
"""

from __future__ import annotations

import logging
import os
import sys

_FORMAT = "%(asctime)s | %(levelname)-3s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: int | None = None) -> None:
    """Configure the root logger for consistent, timestamped stdout output.

    `level` defaults to the `METADATA_MATCHER_LOG_LEVEL` env var (e.g. `DEBUG`
    to see the per-auction START/END traces in metadata_matcher.py), falling
    back to INFO if unset.

    Safe to call more than once (e.g. once from `__main__.py` and again from
    a module that can also run standalone) -- `force=True` just reapplies the
    same configuration.
    """
    if level is None:
        level_name = os.getenv("METADATA_MATCHER_LOG_LEVEL", "INFO").strip().upper()
        level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format=_FORMAT,
        datefmt=_DATEFMT,
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )