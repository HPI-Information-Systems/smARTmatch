from __future__ import annotations

import json
import re
from html import unescape
from datetime import date, datetime
from typing import Any, Iterable, Optional

from .value_parsing import extract_estimate_range, parse_money_amount, to_float


def coalesce(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip() == "":
                continue
            return value.strip()
    return None


def parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.date()
    except Exception:
        pass

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except Exception:
            continue

    return None


def stringify_dict(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def normalize_text(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None

    cleaned = unescape(value).strip()
    if not cleaned:
        return None

    if "<" in cleaned and ">" in cleaned:
        cleaned = re.sub(r"<[^>]+>", "", cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def extract_image_urls(lot_assets: Optional[Iterable[dict[str, Any]]]) -> list[str]:
    if not lot_assets:
        return []

    urls: list[str] = []
    for asset in lot_assets:
        if not isinstance(asset, dict):
            continue

        asset_type = str(asset.get("asset_type") or asset.get("type") or "").lower()
        image_url = asset.get("image_url") or asset.get("url") or asset.get("imageUrl")
        if image_url and (asset_type in {"", "basic", "image"}):
            urls.append(str(image_url))
            continue

        if isinstance(asset.get("sizes"), dict):
            sizes = asset["sizes"]
            for size_key in ("large", "detail", "full", "original", "medium"):
                if size_key in sizes and sizes[size_key]:
                    urls.append(str(sizes[size_key]))
                    break

    return urls


def build_auction_details(lot_data: dict[str, Any], sale_data: dict[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        details[key] = value

    put("saleTitle", coalesce(sale_data.get("title_txt"), sale_data.get("sale_name"), sale_data.get("title")))
    put("saleLocation", coalesce(sale_data.get("sale_location"), sale_data.get("location_txt"), sale_data.get("location")))
    put("saleNumber", coalesce(sale_data.get("sale_number"), sale_data.get("Number")))
    put("saleRoomCode", sale_data.get("sale_room_code"))
    put("saleUrl", sale_data.get("url"))
    put("saleEventType", sale_data.get("event_type"))
    put("saleTimeZone", sale_data.get("time_zone"))
    put("saleStartDate", coalesce(sale_data.get("start_date"), sale_data.get("startDate"), sale_data.get("startDateTime")))
    put("saleEndDate", coalesce(sale_data.get("end_date"), sale_data.get("endDate"), sale_data.get("endDateTime")))
    put("registrationCloseDate", coalesce(sale_data.get("registration_close_date"), sale_data.get("registrationCloseDate")))

    put("lotNumber", coalesce(lot_data.get("lot_id_txt"), lot_data.get("lotNumber"), lot_data.get("lot_number"), lot_data.get("lotno")))

    estimate_low, estimate_high = extract_estimate_range(lot_data)
    put("estimateLow", estimate_low)
    put("estimateHigh", estimate_high)
    put("estimateText", coalesce(lot_data.get("lot_estimate_txt"), lot_data.get("estimate_txt")))
    put("estimateVisible", lot_data.get("estimate_visible"))
    put("estimateOnRequest", lot_data.get("estimate_on_request"))
    put("priceOnRequest", lot_data.get("price_on_request"))

    realized_price = parse_money_amount(lot_data.get("realized_price")) or parse_money_amount(lot_data.get("price_realised_txt"))
    current_bid = parse_money_amount(lot_data.get("current_bid")) or parse_money_amount(lot_data.get("current_bid_txt"))
    put("realizedPrice", realized_price)
    put("currentBid", current_bid)

    put("isUnsold", lot_data.get("is_unsold"))
    put("isAuctionOver", lot_data.get("is_auction_over"))
    put("isInProgress", lot_data.get("is_in_progress"))

    return details


def extract_artist_name(lot_data: dict[str, Any]) -> Optional[str]:
    for key in (
        "artist_name",
        "artistName",
        "artist",
        "artist_display_name",
        "artistDisplayName",
        "creator",
        "maker",
        "primary_artist",
        "primaryArtist",
    ):
        value = lot_data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("name") or value.get("displayName")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
        if isinstance(value, list) and value:
            candidate = value[0]
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
            if isinstance(candidate, dict):
                nested = candidate.get("name") or candidate.get("displayName")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()

    return None


def split_person_name(name: str) -> tuple[Optional[str], str]:
    cleaned = " ".join(name.split())
    if not cleaned:
        return None, ""

    if "," in cleaned:
        last, first = [part.strip() for part in cleaned.split(",", 1)]
        return (first or None), last or cleaned

    parts = cleaned.split(" ")
    if len(parts) == 1:
        return None, parts[0]

    return " ".join(parts[:-1]), parts[-1]


def extract_dimensions(lot_data: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    width = lot_data.get("width")
    height = lot_data.get("height")
    if isinstance(width, (int, float)) or isinstance(height, (int, float)):
        return _to_float(width), _to_float(height)

    dim_text = lot_data.get("dimensions") or ""
    if not isinstance(dim_text, str):
        return None, None

    match = re.search(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)", dim_text)
    if match:
        return _to_float(match.group(1)), _to_float(match.group(2))

    return None, None


def _to_float(value: Any) -> Optional[float]:
    return to_float(value)
