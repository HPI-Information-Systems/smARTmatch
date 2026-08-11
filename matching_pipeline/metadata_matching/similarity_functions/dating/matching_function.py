"""
This module implements a similarity function for dating attributes.

The similarity function checks for any overlap between two dating intervals.
Returns 1.0 if intervals intersect, 0.0 if they don't, and None if data is missing.
"""

def similarity_function(
    lost_start: int, lost_end: int, auc_start: int, auc_end: int
) -> float | None:
    
    CURRENT_YEAR = 2026
    BUFFER = 200

    if (lost_start is None and lost_end is None) or (
        auc_start is None and auc_end is None
    ):
        return None

    # Fill a missing bound using the buffer, capped at the current year.
    def sanitize(start, end):
        if start is None:
            start = end - BUFFER

        if end is None:
            end = min(CURRENT_YEAR, start + BUFFER)

        return start, end

    l_start, l_end = sanitize(lost_start, lost_end)
    a_start, a_end = sanitize(auc_start, auc_end)

    return 1.0 if max(l_start, a_start) <= min(l_end, a_end) else 0.0
