# run all matching test scripts with: python -m pytest -q tests/matching
from __future__ import annotations
import pytest

from matching_pipeline.metadata_matching.similarity_functions.title.matching_function import similarity_function as title_similarity
from matching_pipeline.metadata_matching.similarity_functions.artist.matching_function import similarity_function as artist_similarity
from matching_pipeline.metadata_matching.similarity_functions.dating.matching_function import similarity_function as dating_similarity
from matching_pipeline.metadata_matching.similarity_functions.dimensions.matching_function import similarity_function as dimensions_similarity
from conftest import load_fixture_csv, nullable_float, nullable_int, parse_pg_array

class NoopCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *args, **kwargs):
        raise AssertionError("Variant lookup/update should not run when dict values are already present")

    def fetchall(self):
        return []


class NoopConnection:
    def cursor(self):
        return NoopCursor()


def test_artist_similarity_returns_none_for_unknown_marker():
    assert artist_similarity("unbekannt", "Ernst Frank") is None
    assert artist_similarity(" Unbekannt ", "Ernst Frank") is None
    assert artist_similarity("Ernst Frank", "") is None
    assert artist_similarity("Ernst Frank", "Ernst Frank") == pytest.approx(1.0)
    assert artist_similarity("Adam de Clerck", "Adam de Clerk") > 0.95


@pytest.mark.parametrize(
    "lost_start,lost_end,auc_start,auc_end,expected",
    [
        (1821, 1821, 1821, 1821, 1.0),
        (1821, 1821, 1921, 1921, 0.0),
        (None, None, 1821, 1821, None),
    ],
)
def test_dating_similarity_boundaries(lost_start, lost_end, auc_start, auc_end, expected):
    score = dating_similarity(lost_start, lost_end, auc_start, auc_end)
    if expected is None:
        assert score is None
    else:
        assert score == pytest.approx(expected)


def test_dimensions_similarity_compares_width_and_height_positionally():
    assert dimensions_similarity(40.0, 50.0, 40.0, 50.0) == pytest.approx(1.0)
    # width/height are compared positionally, not orientation-invariant: a
    # portrait-vs-landscape swap does NOT count as a match.
    assert dimensions_similarity(40.0, 50.0, 50.0, 40.0) == pytest.approx(0.0)
    assert dimensions_similarity(None, 50.0, 40.0, 50.0) is None
    assert dimensions_similarity(None, None, 40.0, 50.0) is None


def test_title_similarity_uses_fixture_positive_pair():
    lost = {row["lost_artwork_id"]: row for row in load_fixture_csv("10_lostart.csv")}
    auction = {row["lost_artwork_id"]: row for row in load_fixture_csv("10_spsg.csv")}

    # exact title equality should dominate for the Burgruine fixture.
    assert title_similarity(lost["match102"]["title"], auction["match102"]["title"]) == pytest.approx(1.0)
    assert title_similarity(lost["match102"]["title"], auction["nonmatch106"]["title"]) < 0.3
    assert title_similarity(lost["match102"]["title"], '') is None
    assert title_similarity(None, auction["nonmatch106"]["title"]) is None



def test_fixture_numeric_fields_are_consumable_by_similarity_functions():
    lost = {row["lost_artwork_id"]: row for row in load_fixture_csv("10_lostart.csv")}
    auction = {row["lost_artwork_id"]: row for row in load_fixture_csv("10_spsg.csv")}

    lost_row = lost["match100"]
    auction_row = auction["match100"]

    assert dimensions_similarity(
        nullable_float(lost_row["width"]),
        nullable_float(lost_row["height"]),
        nullable_float(auction_row["width"]),
        nullable_float(auction_row["height"]),
    ) == pytest.approx(1.0)

    assert dating_similarity(
        nullable_int(lost_row["dating_start"]),
        nullable_int(lost_row["dating_end"]),
        nullable_int(auction_row["dating_start"]),
        nullable_int(auction_row["dating_end"]),
    ) == pytest.approx(1.0)
