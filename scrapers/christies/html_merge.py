from __future__ import annotations

import json
import re
from typing import Any, Optional

_MERGED_HTML_FIELDS = ["description", "preLotText", "details", "provenance", "literature", "exhibited"]


def merge_html_fields(lot: dict[str, Any], html_details: dict[str, Any]) -> None:
    for field in _MERGED_HTML_FIELDS:
        if field in html_details:
            lot[field] = html_details[field]


def merge_bri_sale_data(html: str, sale: dict[str, Any]) -> None:
    bri_match = re.search(r"window\.briDataModel\s*=\s*(\{.+?\});", html, re.DOTALL)
    if not bri_match:
        return

    try:
        bri_data = json.loads(bri_match.group(1))
    except Exception:
        return

    sale_data = bri_data.get("saleData")
    if not isinstance(sale_data, dict):
        return

    sale_number = sale_data.get("saleNumber")
    if sale_number:
        sale["sale_number"] = sale_number
        sale.setdefault("number", sale_number)

    sale_id = sale_data.get("saleId")
    if sale_id:
        sale["sale_id"] = sale_id
        sale.setdefault("id", sale_id)


def fallback_payload_from_html_details(html_details: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not html_details:
        return None

    lot: dict[str, Any] = {}
    for field in [
        "title_txt",
        "artistName",
        "artist_name",
        "description",
        "preLotText",
        "details",
        "provenance",
        "literature",
        "exhibited",
    ]:
        value = html_details.get(field)
        if value:
            lot[field] = value

    if not lot:
        return None

    return {
        "lots": lot,
        "sale": {},
        "chr-specialist": html_details.get("chr-specialist"),
    }
