"""Compatibility import for the repository-wide logging adapter.

New code imports from :mod:`shared.logging_adapter` directly.  This module is
kept temporarily for callers outside the container images that may still use
the old matching-pipeline path.
"""

from shared.logging_adapter import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger"]
