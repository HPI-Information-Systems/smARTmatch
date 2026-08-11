from __future__ import annotations

from typing import Optional


class LottissimoArtistMixin:
    """Title/artist handling for Lot-tissimo.

    Policy: do not infer artist/title splits from punctuation heuristics.
    If the title string is comma-delimited (ambiguous artist/title mixture),
    default to missing title instead of guessing.
    """

    def _split_title_and_artist(self, raw_title: str) -> tuple[Optional[str], Optional[str]]:
        normalized_title = " ".join(raw_title.split())
        if not normalized_title:
            return None, None

        if "," in normalized_title:
            return None, None

        return normalized_title, None
