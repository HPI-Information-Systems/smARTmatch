"""Formatting helpers for frontend statistics."""

from __future__ import annotations


def format_int(value):
    return f"{int(value or 0):,}".replace(",", ".")


def format_bytes(byte_count):
    size = float(byte_count or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            precision = 0 if unit == "B" else 1
            return f"{size:.{precision}f} {unit}".replace(".0 ", " ")
        size /= 1024
    return "0 B"
