"""Replication-closure construction and graph-specific hashing."""

from typing import Any, Mapping, Sequence

from telemetry.sync_budget import _ClosureMaterializationBudget
from telemetry.sync_queries import (
    _fetch_entities,
    _fetch_integer_entities,
    _fetch_link_rows,
)
from telemetry.sync_utils import _canonical_hash, _match_key, _values


def _build_data_content(
    conn,
    match_rows: Sequence[Mapping[str, Any]],
    extra_lost_ids: set[str],
    extra_auction_ids: set[str],
    *,
    materialization_budget: _ClosureMaterializationBudget | None = None,
) -> dict[str, Any]:
    lost_ids = _values(match_rows, "lost_id") | set(extra_lost_ids)
    auction_ids = _values(match_rows, "auction_id") | set(extra_auction_ids)
    lost = _fetch_entities(
        conn,
        "lost_artwork",
        "lost_artwork_id",
        lost_ids,
        materialization_budget=materialization_budget,
    )
    auction = _fetch_entities(
        conn,
        "auction_artwork",
        "auction_artwork_id",
        auction_ids,
        materialization_budget=materialization_budget,
    )

    artist_ids = set()
    for row in lost.values():
        artist_ids.update(str(value) for value in (row.get("artist_ids") or []))
    artist_ids.update(_values(auction.values(), "artist_id"))
    artists = _fetch_entities(
        conn,
        "artist",
        "artist_id",
        artist_ids,
        materialization_budget=materialization_budget,
    )
    institutions = _fetch_entities(
        conn,
        "institution",
        "institution_id",
        _values(lost.values(), "institution_id"),
        materialization_budget=materialization_budget,
    )
    literature = _fetch_entities(
        conn,
        "literature_source",
        "literature_id",
        _values(lost.values(), "literature_source_id"),
        materialization_budget=materialization_budget,
    )
    platforms = _fetch_entities(
        conn,
        "auction_platform",
        "auction_platform_id",
        _values(auction.values(), "auction_platform_id"),
        materialization_budget=materialization_budget,
    )
    auctioneers = _fetch_entities(
        conn,
        "auctioneer",
        "auctioneer_id",
        _values(auction.values(), "auctioneer_id"),
        materialization_budget=materialization_budget,
    )
    experts = _fetch_entities(
        conn,
        "expert",
        "expert_id",
        _values(auction.values(), "expert_id"),
        materialization_budget=materialization_budget,
    )
    program_ids = _values(match_rows, "metadata_matching_program") | _values(
        match_rows, "image_matching_program"
    )
    programs = _fetch_entities(
        conn,
        "matching_program",
        "matching_program_id",
        program_ids,
        materialization_budget=materialization_budget,
    )
    location_ids = (
        _values(auction.values(), "artist_birth_place")
        | _values(auction.values(), "artist_death_place")
        | _values(artists.values(), "place_of_birth")
        | _values(artists.values(), "place_of_death")
        | _values(literature.values(), "publishing_location_id")
    )
    locations = _fetch_entities(
        conn,
        "location",
        "location_id",
        location_ids,
        materialization_budget=materialization_budget,
    )

    lost_links = _fetch_link_rows(
        conn,
        "lost_artwork_image_file",
        "lost_artwork_id",
        lost_ids,
        materialization_budget=materialization_budget,
    )
    auction_links = _fetch_link_rows(
        conn,
        "auction_artwork_image_file",
        "auction_artwork_id",
        auction_ids,
        materialization_budget=materialization_budget,
    )
    image_ids = {
        int(row["image_file_id"])
        for row in (*lost_links, *auction_links)
        if row.get("image_file_id") is not None
    }
    image_ids.update(
        int(value)
        for value in (row.get("best_image_file_id") for row in match_rows)
        if value is not None
    )
    image_files = _fetch_integer_entities(
        conn,
        image_ids,
        materialization_budget=materialization_budget,
    )

    match_dict = {
        _match_key(str(row["lost_id"]), str(row["auction_id"])): dict(row)
        for row in match_rows
    }
    content = {
        "entities": {
            "location": locations,
            "artist": artists,
            "institution": institutions,
            "literature_source": literature,
            "auction_platform": platforms,
            "auctioneer": auctioneers,
            "expert": experts,
            "matching_program": programs,
            "image_file": image_files,
            "lost_artwork": lost,
            "auction_artwork": auction,
        },
        "rows": {
            "lost_artwork_image_file": lost_links,
            "auction_artwork_image_file": auction_links,
            "match_score": list(match_dict.values()),
        },
        "hashes": {},
    }
    content["hashes"] = _replication_graph_hashes(content)
    return content


def _replication_graph_hashes(content: Mapping[str, Any]) -> dict[str, Any]:
    entities = content["entities"]
    rows = content["rows"]
    lost_links = rows.get("lost_artwork_image_file") or []
    auction_links = rows.get("auction_artwork_image_file") or []

    def selected(entity_type: str, identifiers: Sequence[Any]) -> dict[str, Any]:
        collection = entities.get(entity_type) or {}
        keys = sorted({str(value) for value in identifiers if value is not None})
        return {key: collection[key] for key in keys if key in collection}

    def images_for(link_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return selected(
            "image_file",
            [row.get("image_file_id") for row in link_rows],
        )

    lost_hashes: dict[str, str] = {}
    for entity_id, artwork in (entities.get("lost_artwork") or {}).items():
        artwork_links = [
            row for row in lost_links if str(row.get("lost_artwork_id")) == entity_id
        ]
        artists = selected("artist", artwork.get("artist_ids") or [])
        literature = selected(
            "literature_source", [artwork.get("literature_source_id")]
        )
        locations = selected(
            "location",
            [
                value
                for artist in artists.values()
                for value in (
                    artist.get("place_of_birth"),
                    artist.get("place_of_death"),
                )
            ]
            + [row.get("publishing_location_id") for row in literature.values()],
        )
        lost_hashes[entity_id] = _canonical_hash(
            {
                "artwork": artwork,
                "artists": artists,
                "institutions": selected(
                    "institution", [artwork.get("institution_id")]
                ),
                "literature_sources": literature,
                "locations": locations,
                "image_links": artwork_links,
                "image_files": images_for(artwork_links),
            }
        )

    auction_hashes: dict[str, str] = {}
    for entity_id, artwork in (entities.get("auction_artwork") or {}).items():
        artwork_links = [
            row
            for row in auction_links
            if str(row.get("auction_artwork_id")) == entity_id
        ]
        artists = selected("artist", [artwork.get("artist_id")])
        locations = selected(
            "location",
            [artwork.get("artist_birth_place"), artwork.get("artist_death_place")]
            + [
                value
                for artist in artists.values()
                for value in (
                    artist.get("place_of_birth"),
                    artist.get("place_of_death"),
                )
            ],
        )
        auction_hashes[entity_id] = _canonical_hash(
            {
                "artwork": artwork,
                "artists": artists,
                "locations": locations,
                "auction_platforms": selected(
                    "auction_platform", [artwork.get("auction_platform_id")]
                ),
                "auctioneers": selected("auctioneer", [artwork.get("auctioneer_id")]),
                "experts": selected("expert", [artwork.get("expert_id")]),
                "image_links": artwork_links,
                "image_files": images_for(artwork_links),
            }
        )

    match_hashes: dict[str, str] = {}
    for match in rows.get("match_score") or []:
        key = _match_key(str(match["lost_id"]), str(match["auction_id"]))
        match_hashes[key] = _canonical_hash(
            {
                "match_score": match,
                "matching_programs": selected(
                    "matching_program",
                    [
                        match.get("metadata_matching_program"),
                        match.get("image_matching_program"),
                    ],
                ),
                "best_image_file": selected(
                    "image_file", [match.get("best_image_file_id")]
                ),
            }
        )
    return {
        "match_score": match_hashes,
        "lost_artwork": lost_hashes,
        "auction_artwork": auction_hashes,
    }
