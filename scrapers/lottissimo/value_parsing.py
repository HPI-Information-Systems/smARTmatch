from __future__ import annotations

import re
from datetime import date
from typing import Optional


def normalize_lot_number(value: Optional[str]) -> Optional[str]:
    text = " ".join((value or "").split())
    if not text:
        return None

    text = re.sub(r"^(?:los|lot)\s*", "", text, flags=re.IGNORECASE)
    text = text.strip("#:- ")
    if not text:
        return None

    if len(text) > 24:
        return None
    if not re.search(r"\d", text):
        return None
    if re.search(r"[A-Za-zÀ-ÿ]{5,}", text):
        return None

    return text


def parse_iso_date(value: Optional[str]) -> Optional[date]:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_decimal(value: Optional[str]) -> Optional[float]:
    text = (value or "").strip()
    if not text:
        return None

    text = re.sub(r"[^0-9,.-]", "", text)
    if not text:
        return None

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(".", "").replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: Optional[str]) -> Optional[int]:
    text = (value or "").strip()
    if not text:
        return None
    cleaned = re.sub(r"[^0-9-]", "", text)
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_bool(value: Optional[str]) -> Optional[bool]:
    text = (value or "").strip().casefold()
    if not text:
        return None
    if text in {"true", "1", "yes", "ja"}:
        return True
    if text in {"false", "0", "no", "nein"}:
        return False
    return None
