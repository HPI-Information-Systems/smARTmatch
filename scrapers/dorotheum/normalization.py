from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from html import unescape
from typing import Optional
from urllib.parse import urljoin

from .constants import ROOT_URL


class DorotheumNormalizationMixin:
    @staticmethod
    def _clean_text(value: object) -> Optional[str]:
        if value is None:
            return None
        text = " ".join(str(value).split()).strip()
        return text or None

    def _pick_text(self, *values: object) -> Optional[str]:
        for value in values:
            text = self._clean_text(value)
            if not text:
                continue
            if text.casefold() == "undefined":
                continue
            return text
        return None

    @staticmethod
    def _load_json(value: object) -> object:
        if value is None:
            return None

        text = str(value).strip()
        if not text:
            return None

        try:
            return json.loads(unescape(text))
        except json.JSONDecodeError:
            return None

    def _parse_amount(self, value: object) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)

        text = self._clean_text(value)
        if not text:
            return None

        cleaned = re.sub(r"[^0-9,.-]", "", text).rstrip("-")
        if not cleaned:
            return None

        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")

        try:
            return float(cleaned)
        except ValueError:
            return None

    def _parse_datetime_to_date(self, value: Optional[str]) -> Optional[date]:
        text = self._clean_text(value)
        if not text:
            return None

        date_part = text.split(" ", 1)[0]
        for fmt in (None, "%d.%m.%Y"):
            try:
                if fmt is None:
                    return datetime.fromisoformat(date_part).date()
                return datetime.strptime(date_part, fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _epoch_to_date(value: object) -> Optional[date]:
        if not isinstance(value, (int, float)):
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).date()
        except (OverflowError, ValueError, OSError):
            return None

    @staticmethod
    def _coerce_int(value: object) -> Optional[int]:
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return None

    @staticmethod
    def _normalize_urls(value: object) -> list[str]:
        if not isinstance(value, list):
            return []

        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue

            full_url = urljoin(ROOT_URL, item)
            if full_url in seen:
                continue
            seen.add(full_url)
            out.append(full_url)
        return out

    def _clean_artist_name(self, value: object) -> Optional[str]:
        text = self._clean_text(value)
        if not text:
            return None

        cleaned = re.sub(r"\s*[\*+#-]+$", "", text).strip()
        if not cleaned:
            return None

        lowered = cleaned.casefold()
        if lowered == "undefined":
            return None
        if lowered.startswith("künstler"):
            return None
        if lowered.startswith("unbekannter") or lowered.startswith("anonym"):
            return None

        return cleaned

    @staticmethod
    def _normalize_identity_token(value: str) -> str:
        trimmed = re.sub(r"\s*[\*+#-]+$", "", value).strip()
        without_parentheses = re.sub(r"\([^)]*\)", "", trimmed)
        squashed = re.sub(r"\s+", " ", without_parentheses).strip()
        return squashed.casefold()

    def _same_identity_text(self, left: Optional[str], right: Optional[str]) -> bool:
        if not left or not right:
            return False
        return self._normalize_identity_token(left) == self._normalize_identity_token(right)

    def _normalize_title(self, value: object, *, artist_name: Optional[str]) -> Optional[str]:
        text = self._clean_text(value)
        if not text:
            return None

        lowered = text.casefold()
        if lowered in {"undefined", "ohne titel", "untitled", "n/a"}:
            return None

        # Dorotheum's headline often carries the artist label. Avoid storing
        # a likely-incorrect title when no explicit artist field is available.
        if artist_name is None:
            return None

        if self._same_identity_text(text, artist_name):
            return None

        return text
