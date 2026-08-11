from __future__ import annotations

import re
from typing import Any, Optional


def to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def parse_money_amount(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    numbers = re.findall(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text)
    if not numbers:
        return None

    try:
        return float(numbers[0].replace(",", ""))
    except Exception:
        return None


def extract_estimate_range(lot_data: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    low = parse_money_amount(lot_data.get("estimate_low"))
    high = parse_money_amount(lot_data.get("estimate_high"))
    if low is not None or high is not None:
        return low, high

    estimate_text = str(lot_data.get("lot_estimate_txt") or lot_data.get("estimate_txt") or "").strip()
    if not estimate_text:
        return None, None

    lowered = estimate_text.casefold()
    if "request" in lowered:
        return None, None

    numbers = re.findall(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", estimate_text)
    values: list[float] = []
    for number in numbers:
        try:
            values.append(float(number.replace(",", "")))
        except Exception:
            continue

    if len(values) >= 2:
        return values[0], values[1]
    if len(values) == 1:
        return values[0], values[0]
    return None, None
