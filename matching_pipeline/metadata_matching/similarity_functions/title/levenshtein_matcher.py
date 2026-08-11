""""
This module implements a similarity function for title score counting.

It uses a levenshtein matcher to compute a score between two titles
and also optimize the scores by removing common words like 
"portrait" or "landscape" from both titles before computing the score.
"""

import re
from typing import Optional
from rapidfuzz.distance import Levenshtein

WORDS_TO_REMOVE = [
    "portait",
    "portrait",
    "porträt",
    "portret",
    "bildnis",
    "brustbild",
    "landschaft",
    "landschaften",
]

_WORD_PATTERNS = {
    word: re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)", flags=re.IGNORECASE)
    for word in WORDS_TO_REMOVE
}
_SPACE_PATTERN = re.compile(r"\s+")


def contains_word(text: str, word: str) -> bool:
    return bool(_WORD_PATTERNS[word].search(text.lower()))


def remove_words(text: str, words: list[str]) -> str:
    text = text.lower().strip()

    if not words:
        return text

    for word in words:
        text = _WORD_PATTERNS[word].sub(" ", text)

    text = _SPACE_PATTERN.sub(" ", text).strip()

    return text


def normalize_titles_pair(a: str, b: str) -> tuple[str, str]:
    a = a.lower().strip()
    b = b.lower().strip()

    common_words = [
        word
        for word in WORDS_TO_REMOVE
        if contains_word(a, word) and contains_word(b, word)
    ]

    a_candidate = remove_words(a, common_words)
    b_candidate = remove_words(b, common_words)

    if not a_candidate or not b_candidate:
        return a, b

    return a_candidate, b_candidate


def similarity_function(a: str, b: str) -> Optional[float]:
    if not a or a.strip() == "" or not b or b.strip() == "":
        return None

    a_norm, b_norm = normalize_titles_pair(a, b)

    return Levenshtein.normalized_similarity(a_norm, b_norm)