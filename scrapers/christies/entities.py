from __future__ import annotations

from typing import Optional

from ..db_interface import Database
from ..utils.auction_helpers import json_dumps
from .parser import split_person_name


def resolve_expert_id(db: Database, specialist: Optional[dict[str, object]]) -> Optional[str]:
    if not specialist or not specialist.get("name"):
        return None

    specialist_name = str(specialist["name"])
    first_name, last_name = split_person_name(specialist_name)
    expert = db.get_or_create_expert(
        first_name=first_name or "",
        last_name=last_name or specialist_name,
        organization=specialist.get("category"),
        raw_data=json_dumps(specialist),
    )
    return expert.expert_id


def resolve_artist_id(db: Database, artist_name: Optional[str]) -> Optional[str]:
    if not artist_name:
        return None

    cleaned = artist_name.strip()
    if not cleaned:
        return None

    artist = db.get_or_create_artist(
        complete_name=cleaned,
        raw_data=json_dumps({"name": cleaned}),
    )
    return artist.artist_id
