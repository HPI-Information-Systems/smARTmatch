from __future__ import annotations

from datetime import date
from typing import Optional

from ..utils.auction_helpers import json_dumps
from .constants import SOTHEBYS_ORIGIN
from .value_parsing import clean_text, parse_amount, parse_iso_datetime

def pick_best_rendition_url(renditions: list[dict]) -> Optional[str]:
    if not renditions:
        return None

    def score(rendition: dict) -> tuple[int, int]:
        size = (rendition.get("imageSize") or "").lower()
        preference = {
            "extraextralarge": 5,
            "extra_large": 4,
            "extralarge": 4,
            "large": 3,
            "medium": 2,
            "small": 1,
        }.get(size, 0)
        width = rendition.get("width")
        height = rendition.get("height")
        area = int(width) * int(height) if isinstance(width, (int, float)) and isinstance(height, (int, float)) else 0
        return preference, area

    best = max(renditions, key=score)
    url = best.get("url")
    return url if isinstance(url, str) and url else None


def extract_lot_block(response: dict) -> Optional[dict]:
    data = response.get("data")
    if not isinstance(data, dict):
        return None

    lot = data.get("lot")
    return lot if isinstance(lot, dict) else None


def extract_lot_id(lot: dict) -> Optional[str]:
    return clean_text(lot.get("lotId"))


def extract_auction_block(lot: dict) -> dict:
    auction = lot.get("auction")
    return auction if isinstance(auction, dict) else {}


def extract_lot_number_info(lot: dict) -> tuple[Optional[str], Optional[str], Optional[bool]]:
    lot_number_obj = lot.get("lotNumber")
    if not isinstance(lot_number_obj, dict):
        return None, None, None

    lot_number = clean_text(lot_number_obj.get("lotNumber"))
    lot_number_type = clean_text(lot_number_obj.get("__typename"))

    lot_number_visible = None
    if lot_number_type:
        lowered = lot_number_type.casefold()
        if lowered == "visiblelotnumber":
            lot_number_visible = True
        elif lowered == "hiddenlotnumber":
            lot_number_visible = False
    elif lot_number:
        lot_number_visible = True

    return lot_number, lot_number_type, lot_number_visible


def extract_estimate_info(lot: dict) -> tuple[Optional[float], Optional[float], Optional[str], Optional[bool]]:
    estimate = lot.get("estimateV2")
    if not isinstance(estimate, dict):
        return None, None, None, None

    estimate_type = clean_text(estimate.get("__typename"))

    low_amount = None
    low = estimate.get("lowEstimate")
    if isinstance(low, dict):
        low_amount = parse_amount(clean_text(low.get("amount")))

    high_amount = None
    high = estimate.get("highEstimate")
    if isinstance(high, dict):
        high_amount = parse_amount(clean_text(high.get("amount")))

    estimate_upon_request = None
    if estimate_type:
        estimate_upon_request = estimate_type.casefold() == "estimateuponrequest"
    elif low_amount is not None or high_amount is not None:
        estimate_upon_request = False

    return low_amount, high_amount, estimate_type, estimate_upon_request


def extract_auction_dates(auction: dict) -> dict[str, Optional[str]]:
    dates = auction.get("dates")
    if not isinstance(dates, dict):
        return {
            "acceptsBids": None,
            "goesLive": None,
            "published": None,
            "closed": None,
        }

    return {
        "acceptsBids": clean_text(dates.get("acceptsBids")),
        "goesLive": clean_text(dates.get("goesLive")),
        "published": clean_text(dates.get("published")),
        "closed": clean_text(dates.get("closed")),
    }


def extract_auction_date(auction_dates: dict[str, Optional[str]]) -> Optional[date]:
    dt = (
        parse_iso_datetime(auction_dates.get("goesLive"))
        or parse_iso_datetime(auction_dates.get("acceptsBids"))
        or parse_iso_datetime(auction_dates.get("published"))
    )
    return dt.date() if dt else None


def extract_departments(auction: dict) -> Optional[str]:
    departments = auction.get("departmentNames")
    if not isinstance(departments, list):
        return None

    cleaned = [dept.strip() for dept in departments if isinstance(dept, str) and dept.strip()]
    if not cleaned:
        return None
    return ", ".join(cleaned)


def extract_auction_year(auction: dict) -> Optional[str]:
    slug = auction.get("slug")
    if not isinstance(slug, dict):
        return None

    year = slug.get("year")
    if isinstance(year, int):
        return str(year)
    if isinstance(year, str):
        return year.strip() or None
    return None


def extract_auction_slug_name(auction: dict) -> Optional[str]:
    slug = auction.get("slug")
    if not isinstance(slug, dict):
        return None
    return clean_text(slug.get("name"))


def build_lot_url(*, lot: dict, auction: dict) -> Optional[str]:
    lot_slug = clean_text(lot.get("slug"))
    auction_name = extract_auction_slug_name(auction)
    auction_year = extract_auction_year(auction)
    if not lot_slug or not auction_name or not auction_year:
        return None
    return f"{SOTHEBYS_ORIGIN}/en/buy/auction/{auction_year}/{auction_name}/{lot_slug}"


def extract_image_urls(lot: dict) -> list[str]:
    media = lot.get("media")
    if not isinstance(media, dict):
        return []

    images = media.get("images")
    if not isinstance(images, list):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for image in images:
        if not isinstance(image, dict):
            continue

        renditions = image.get("renditions")
        if not isinstance(renditions, list):
            continue

        url = pick_best_rendition_url(renditions)
        if not url or url in seen:
            continue

        seen.add(url)
        out.append(url)

    return out


def build_raw_payload(response: dict) -> str:
    payload = {
        "source": "sothebys",
        "lot_response": response,
    }
    return json_dumps(payload)
