from __future__ import annotations


def normalize_text_line(value: str) -> str:
    return " ".join((value or "").split()).strip()
