from __future__ import annotations

import re
from datetime import datetime
from html import unescape
from typing import Optional


def clean_text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None

    cleaned = unescape(value)
    if "<" in cleaned and ">" in cleaned:
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None

    text = value.strip().replace("Z", "+00:00")
    if not text:
        return None

    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def parse_amount(value: Optional[str]) -> Optional[float]:
    if not value or not isinstance(value, str):
        return None

    text = value.strip().replace(",", "")
    if not text:
        return None

    try:
        return float(text)
    except Exception:
        return None
