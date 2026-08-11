"""Search helpers for the SmartMatch frontend match list."""

import re


def clean_search_value(value):
    """Normalize a request search parameter for display and querying."""
    search = str(value or "").strip()
    return re.sub(r"\s+", " ", search)
