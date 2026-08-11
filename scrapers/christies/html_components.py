from __future__ import annotations

import json
import re
from typing import Any, Optional

from .html_details import extract_html_details
from .html_merge import fallback_payload_from_html_details, merge_bri_sale_data, merge_html_fields


def extract_chr_components(html: str) -> Optional[dict[str, Any]]:
    try:
        start_match = re.search(r"window\.chrComponents\s*=\s*", html)
        if not start_match:
            return None

        start_pos = start_match.end()
        brace_count = 0
        i = start_pos
        in_string = False
        escape_next = False

        while i < len(html):
            char = html[i]

            if escape_next:
                escape_next = False
                i += 1
                continue

            if char == "\\":
                escape_next = True
                i += 1
                continue

            if char == '"':
                in_string = not in_string
                i += 1
                continue

            if not in_string:
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        return json.loads(html[start_pos : i + 1])

            i += 1
    except Exception:
        return None

    return None


def extract_lot_header_data(html: str) -> Optional[dict[str, Any]]:
    html_details = extract_html_details(html)

    lot_header_data = _extract_lot_header_assignment(html)
    if lot_header_data:
        payload = _lot_header_payload(lot_header_data, html_details)
        if payload:
            return payload

    chr_components = extract_chr_components(html)
    if chr_components:
        payload = _components_payload(chr_components, html, html_details)
        if payload:
            return payload

    return fallback_payload_from_html_details(html_details)


def _extract_lot_header_assignment(html: str) -> Optional[dict[str, Any]]:
    match = re.search(r"window\.chrComponents\.lotHeader_\d+\s*=\s*(\{.+?\});", html, re.DOTALL)
    if not match:
        return None

    try:
        return json.loads(match.group(1))
    except Exception:
        return None


def _lot_header_payload(lot_header: dict[str, Any], html_details: dict[str, Any]) -> Optional[dict[str, Any]]:
    data = lot_header.get("data")
    if not isinstance(data, dict):
        return None

    lots = data.get("lots")
    if not isinstance(lots, list) or not lots or not isinstance(lots[0], dict):
        return None

    lot = lots[0].copy()
    sale = data.get("sale", {})
    sale_copy = sale.copy() if isinstance(sale, dict) else {}
    merge_html_fields(lot, html_details)

    return {
        "lots": lot,
        "sale": sale_copy,
        "chr-specialist": html_details.get("chr-specialist"),
    }


def _components_payload(
    chr_components: dict[str, Any],
    html: str,
    html_details: dict[str, Any],
) -> Optional[dict[str, Any]]:
    lots_container = chr_components.get("lots")
    if not isinstance(lots_container, dict):
        return None

    lots_data = lots_container.get("data")
    if not isinstance(lots_data, dict):
        return None

    lots = lots_data.get("lots")
    if not isinstance(lots, list) or not lots or not isinstance(lots[0], dict):
        return None

    lot = lots[0].copy()
    sale = lot.get("sale", {})
    sale_copy = sale.copy() if isinstance(sale, dict) else {}

    auction = chr_components.get("auction")
    if isinstance(auction, dict) and isinstance(auction.get("data"), dict):
        auction_data = auction["data"]
        for key in ["sale_id", "sale_number", "sale_room_code", "sale_location"]:
            value = auction_data.get(key)
            if value:
                sale_copy[key] = value

    merge_bri_sale_data(html, sale_copy)
    merge_html_fields(lot, html_details)

    return {
        "lots": lot,
        "sale": sale_copy,
        "chr-specialist": html_details.get("chr-specialist"),
    }
