from __future__ import annotations

from typing import Optional

from .constants import LOT_NUMBER_PREFIX_RE
from .title_utils import normalize_text_line


class DrouotArtistMixin:
    def _clean_display_title(self, value: str) -> str:
        cleaned = LOT_NUMBER_PREFIX_RE.sub("", value or "")
        normalized = normalize_text_line(cleaned)
        return normalized.strip(" ,.-;:|")

    @staticmethod
    def _normalize_explicit_artist(value: Optional[str]) -> Optional[str]:
        text = normalize_text_line(value or "")
        if not text:
            return None

        lowered = text.casefold()
        if lowered in {"undefined", "unknown", "n/a", "na"}:
            return None
        if "nicht identifizierte signatur" in lowered:
            return None
        if "signature non identifiée" in lowered or "signature non identifiee" in lowered:
            return None
        if "unidentified signature" in lowered:
            return None

        return text

    # Backward-compat helper retained for tests; strict mode no longer infers
    # artist/title from description patterns.
    def _derive_artist_and_title(self, display_title: str, description: str) -> tuple[str, Optional[str]]:
        return self._clean_display_title(display_title), None
