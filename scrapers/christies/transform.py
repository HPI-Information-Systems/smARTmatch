from __future__ import annotations

from typing import Optional

from .models import ChristiesLotFields
from .parser import (
    coalesce,
    extract_dimensions,
    normalize_text,
    parse_date,
)


def resolve_lot_fields(
    *,
    lot_id: str,
    lot_data: dict[str, object],
    sale_data: dict[str, object],
    page_title: Optional[str],
) -> ChristiesLotFields:
    def pick(source: dict[str, object], *keys: str) -> Optional[str]:
        return coalesce(*(source.get(key) for key in keys))

    payload_artist = normalize_text(pick(lot_data, "title_primary_txt", "artist_name"))
    payload_title = normalize_text(pick(lot_data, "title_secondary_txt", "title_tertiary_txt"))

    title = normalize_text(
        coalesce(
            page_title,
            payload_title,
            pick(lot_data, "title_txt", "title", "name"),
        )
    ) or f"Christie's lot {lot_id}"

    auction_date = parse_date(
        pick(sale_data, "start_date", "startDate", "startDateTime") if sale_data else None
    )

    width, height = extract_dimensions(lot_data)
    return ChristiesLotFields(
        title=title,
        description=pick(lot_data, "description", "description_txt", "details"),
        provenance=lot_data.get("provenance"),
        material=pick(lot_data, "material", "medium"),
        technique=pick(lot_data, "technique", "method"),
        dating=pick(lot_data, "date", "dated", "year", "circa"),
        condition=pick(lot_data, "condition"),
        signature=pick(lot_data, "signature"),
        literature=pick(lot_data, "literature"),
        auction_date=auction_date,
        width=width,
        height=height,
        payload_artist=payload_artist,
    )


def should_log_resolution_debug(*, title: str, artist_name: Optional[str]) -> bool:
    return title.startswith("Christie's lot ") or not artist_name
