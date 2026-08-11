# run all matching test scripts with: python -m pytest -q tests/matching

from __future__ import annotations

import pytest

from conftest import load_fixture_csv, parse_pg_array
from matching_pipeline.metadata_matching.similarity_functions.technique_and_material.load_hierarchy import load_hierarchies
from matching_pipeline.metadata_matching.similarity_functions.technique_and_material.matching_function import (
    similarity_function as technique_material_similarity,
)

POSITIVE_HARD_CASES = [
    ("match100", 0.5, 0.5),
    ("match101", 1.0, 1.0 / 3.0),
    ("match102", 0.5, 1.0),
    ("match103", 1.0, 1.0),
    ("match104", 1.0, 0.5),
    ("match105", 1.0, 0.5),
]

NEGATIVE_HARD_CASES = [
    # lost_id, auction_key, max_material_score, max_technique_score
    ("nonmatch211", "nonmatch107", 0.0, 0.0),
    ("nonmatch214", "nonmatch106", None, 1.0),
    ("nonmatch213", "nonmatch108", 0.0, None),
]


def _fixture_rows(match_id: str) -> tuple[dict[str, str], dict[str, str]]:
    lost = {
        row["lost_artwork_id"]: row
        for row in load_fixture_csv("10_lostart.csv")
    }
    auction = {
        row["lost_artwork_id"]: row
        for row in load_fixture_csv("10_spsg.csv")
    }
    return lost[match_id], auction[match_id]


# similarity_function only ever compares already-resolved dict_material_name /
# dict_technique_name arrays -- resolving raw material/technique text into
# those dict names (with DB variant lookups) is normalization's job, see
# normalization/technique_material_normalization/dict_lookup.py.
@pytest.mark.parametrize(
    "match_id,min_material_score,min_technique_score",
    POSITIVE_HARD_CASES,
)
def test_material_and_technique_scores_from_existing_dict_names(
    match_id: str,
    min_material_score: float,
    min_technique_score: float,
):
    load_hierarchies()
    lost_row, auction_row = _fixture_rows(match_id)

    material_score, technique_score = technique_material_similarity(
        parse_pg_array(lost_row["dict_material_name"]),
        parse_pg_array(lost_row["dict_technique_name"]),
        parse_pg_array(auction_row["dict_material_name"]),
        parse_pg_array(auction_row["dict_technique_name"]),
    )

    assert material_score is not None
    assert technique_score is not None
    assert material_score + 1e-12 >= min_material_score
    assert technique_score + 1e-12 >= min_technique_score


@pytest.mark.parametrize(
    "lost_id,auction_id,max_material_score,max_technique_score",
    NEGATIVE_HARD_CASES,
)
def test_material_and_technique_negative_hard_cases_stay_below_caps(
    lost_id,
    auction_id,
    max_material_score,
    max_technique_score,
):
    load_hierarchies()

    lost = {
        row["lost_artwork_id"]: row
        for row in load_fixture_csv("10_lostart.csv")
    }
    auction = {
        row["lost_artwork_id"]: row
        for row in load_fixture_csv("10_spsg.csv")
    }

    lost_row = lost[lost_id]
    auction_row = auction[auction_id]

    material_score, technique_score = technique_material_similarity(
        parse_pg_array(lost_row["dict_material_name"]),
        parse_pg_array(lost_row["dict_technique_name"]),
        parse_pg_array(auction_row["dict_material_name"]),
        parse_pg_array(auction_row["dict_technique_name"]),
    )

    if max_material_score is None:
        assert material_score is None
    elif material_score is not None:
        assert material_score <= max_material_score + 1e-12

    if max_technique_score is None:
        assert technique_score is None
    elif technique_score is not None:
        assert technique_score <= max_technique_score + 1e-12
