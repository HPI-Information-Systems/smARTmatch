"""
This module implements Jaro-Winkler similarity function for artist names.

Best for short strings with minor typos or transpositions,
for example names and titles with small spelling differences.
"""

from typing import Optional
from rapidfuzz.distance import JaroWinkler


def similarity_function(a: Optional[str], b: Optional[str]) -> Optional[float]:
    """
    Calculate Jaro-Winkler similarity for two strings.

    Returns:
    - None if either input is None or empty
    - float in range [0.0, 1.0] otherwise
    """
    if not a or not b:
        return None

    if a.strip() == "" or b.strip() == "":
        return None

    if a.lower().strip() == 'unbekannt' or b.lower().strip() == 'unbekannt':
        return None
    
    return float(JaroWinkler.normalized_similarity(a.lower().strip(), b.lower().strip()))
