# run all matching test scripts with: python -m pytest -q tests/matching
from __future__ import annotations
import math
from pathlib import Path
from typing import Any
import numpy as np
import pytest

import matching_pipeline.metadata_matching.similarity_functions.artist.count_confidence as artist_conf
import matching_pipeline.metadata_matching.similarity_functions.dating.count_confidence as dating_conf
import matching_pipeline.metadata_matching.similarity_functions.dimensions.count_confidence as dimensions_conf
import matching_pipeline.metadata_matching.similarity_functions.technique_and_material.count_confidence as tm_conf
import matching_pipeline.metadata_matching.similarity_functions.technique_and_material.load_hierarchy as hierarchy
import matching_pipeline.metadata_matching.similarity_functions.title.count_confidence as title_conf

#Confidence modules use global caches/state, so reset them before every test 
# so tests are deterministic and never depend on a developer machine's previous DB-derived cache.
@pytest.fixture(autouse=True)
def reset_confidence_global_state(monkeypatch):
    monkeypatch.setattr(title_conf, "TITLE_COUNT_INDEX", {})
    monkeypatch.setattr(title_conf, "TITLE_INDEX_TOTAL_COUNT", 0)
    monkeypatch.setattr(title_conf, "TITLE_INDEX_LOADED", False)

    monkeypatch.setattr(artist_conf, "ARTIST_COUNT_INDEX", {})
    monkeypatch.setattr(artist_conf, "ARTIST_INDEX_TOTAL_DOCS", 0)
    monkeypatch.setattr(artist_conf, "ARTIST_INDEX_LOADED", False)

    monkeypatch.setattr(dating_conf, "_dating_state", None)
    monkeypatch.setattr(dimensions_conf, "_dim_state", None)


class _TitleIndexCursor:
    def __init__(self, rows: list[tuple[str, int]], total: int):
        self.rows = rows
        self.total = total
        self.executed_sql: list[str] = []
        self._last_query = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: Any = None):
        self._last_query = " ".join(sql.lower().split())
        self.executed_sql.append(self._last_query)

    def fetchall(self):
        assert "group by title" in self._last_query
        return self.rows

    def fetchone(self):
        assert "count" in self._last_query
        return (self.total,)


class _TitleIndexConnection:
    def __init__(self, rows: list[tuple[str, int]], total: int):
        self.cursor_obj = _TitleIndexCursor(rows=rows, total=total)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


class _ArtistIndexCursor:
    def __init__(self, rows: list[tuple[Any]]):
        self.rows = rows
        self.executed_sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: Any = None):
        self.executed_sql.append(" ".join(sql.lower().split()))

    def fetchall(self):
        return self.rows


class _ArtistIndexConnection:
    def __init__(self, rows: list[tuple[Any]]):
        self.cursor_obj = _ArtistIndexCursor(rows)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


class _ContextCursor:
    def __init__(self, rows: list[tuple[Any, ...]]):
        self.rows = rows
        self.executed_sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: Any = None):
        self.executed_sql.append(" ".join(sql.lower().split()))

    def fetchall(self):
        return self.rows


class _ContextConnection:
    def __init__(self, rows: list[tuple[Any, ...]]):
        self.cursor_obj = _ContextCursor(rows)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    def cursor(self):
        return self.cursor_obj


class _DummyKDE:
    def __init__(self, densities_by_point: dict[tuple[float, float], float], default: float = 1e-12):
        self.densities_by_point = densities_by_point
        self.default = default

    def __call__(self, points):
        points = np.asarray(points, dtype=float)
        return np.array(
            [
                self.densities_by_point.get(
                    (float(points[0, idx]), float(points[1, idx])),
                    self.default,
                )
                for idx in range(points.shape[1])
            ],
            dtype=float,
        )


class _FakeGaussianKDE:
    def __init__(self, training_points):
        self.training_points = np.asarray(training_points, dtype=float)

    def __call__(self, points):
        points = np.asarray(points, dtype=float)
        # Deterministic positive densities; length must match the number of columns.
        return np.linspace(0.10, 0.30, points.shape[1], dtype=float)


def test_title_load_title_index_builds_normalized_repeated_title_cache(monkeypatch):
    conn = _TitleIndexConnection(
        rows=[("  Common    title ", 50), ("Rare title", 2)],
        total=100,
    )
    monkeypatch.setattr(title_conf, "_connect", lambda: conn)

    title_conf.load_title_index()

    assert conn.closed is True
    assert title_conf.TITLE_INDEX_LOADED is True
    assert title_conf.TITLE_INDEX_TOTAL_COUNT == 100
    assert title_conf.TITLE_COUNT_INDEX == {
        "Common title": 50,
        "Rare title": 2,
    }

    # Second call must use the in-memory cache and must not hit the DB again.
    title_conf.load_title_index()
    assert len(conn.cursor_obj.executed_sql) == 2


def test_title_confidence_score_handles_missing_unique_common_and_alias_cases():
    title_conf.TITLE_INDEX_LOADED = True
    title_conf.TITLE_INDEX_TOTAL_COUNT = 100
    title_conf.TITLE_COUNT_INDEX = {
        "Common title": 50,
        "Rare title": 2,
    }

    assert title_conf.confidence_function(None, "Some title") == 0.0
    assert title_conf.confidence_function("", "Some title") == 0.0
    assert title_conf.confidence_function("   ", "Some title") == 0.0
    assert title_conf.confidence_function("Some title", None) == 0.0
    assert title_conf.confidence_function("Some title", "") == 0.0

    assert title_conf.confidence_function("Unique title", "Unique title") == pytest.approx(1.0)
    assert title_conf.confidence_function("  Unique   title ", "Unique title") == pytest.approx(1.0)

    common_score = title_conf.confidence_function("Common title", "Common title")
    rare_score = title_conf.confidence_function("Rare title", "Rare title")
    assert 0.0 < common_score < 1.0
    assert 0.0 < rare_score < 1.0
    assert common_score < rare_score < 1.0

    # mixing a rare indexed title with an unindexed (unique) one should land
    # strictly between the rare-vs-rare score and the unique-vs-unique score of 1.0
    mixed_score = title_conf.confidence_function("Rare title", "Unique title")
    assert rare_score < mixed_score < 1.0


def test_artist_load_artist_index_counts_distinct_artist_once_per_document_and_writes_csv(
    monkeypatch,
    tmp_path: Path,
):
    rows = [
        (["artist-a", "artist-b", "artist-a"],),  # duplicate in one artwork counts once
        ("{artist-b, artist-c}",),
        ("artist-c; artist-d",),
        (None,),
        ("",),
    ]
    conn = _ArtistIndexConnection(rows)
    out_csv = tmp_path / "common_artists.csv"

    monkeypatch.setattr(artist_conf, "_connect", lambda: conn)
    monkeypatch.setattr(artist_conf, "COMMON_ARTISTS_CSV", out_csv)

    artist_conf.load_artist_index()

    assert conn.closed is True
    assert artist_conf.ARTIST_INDEX_LOADED is True
    assert artist_conf.ARTIST_INDEX_TOTAL_DOCS == 3
    assert artist_conf.ARTIST_COUNT_INDEX == {
        "artist-a": 1,
        "artist-b": 2,
        "artist-c": 2,
        "artist-d": 1,
    }
    assert out_csv.read_text(encoding="utf-8").splitlines()[0] == (
        "artist_name;artist_count;artist_score"
    )

    # Second call must use the in-memory cache and must not hit the DB again.
    artist_conf.load_artist_index()
    assert len(conn.cursor_obj.executed_sql) == 1


def test_artist_confidence_score_handles_missing_parsing_unique_and_common_ids():
    artist_conf.ARTIST_INDEX_LOADED = True
    artist_conf.ARTIST_INDEX_TOTAL_DOCS = 100
    artist_conf.ARTIST_COUNT_INDEX = {
        "common": 50,
        "rare": 2,
    }

    assert artist_conf.confidence_score(None, "common") == 0.0
    assert artist_conf.confidence_score("common", None) == 0.0

    common_score = artist_conf.confidence_score("common", "common")
    rare_score = artist_conf.confidence_score("rare", "rare")
    assert 0.0 < common_score < 1.0
    assert 0.0 < rare_score < 1.0
    assert artist_conf.confidence_score("not-in-index", "not-in-index") == pytest.approx(1.0)
    assert common_score < rare_score < 1.0


def _set_dating_state():
    log_n = math.log(10)
    dating_conf._dating_state = {
        "coverage": np.array([10, 2, 1, 5], dtype=np.int32),
        "idf": np.array(
            [
                math.log(10 / 10),
                math.log(10 / 2),
                math.log(10 / 1),
                math.log(10 / 5),
            ],
            dtype=float,
        ),
        "year_min": 1900,
        "year_max": 1903,
        "log_N": log_n,
    }

def test_dating_confidence_function_scores_intervals_and_clips_to_observed_year_range():
    _set_dating_state()

    assert dating_conf.confidence_function(None, 1901, 1902, 1902) == 0.0
    assert dating_conf.confidence_function(1901, None, 1902, 1902) == 0.0
    assert dating_conf.confidence_function(1901, 1901, None, 1902) == 0.0
    assert dating_conf.confidence_function(1901, 1901, 1902, None) == 0.0
    assert dating_conf.confidence_function(1501, 1502, 1503, 1504) == pytest.approx(1.0)

def test_dating_load_dating_builds_state_from_database_and_reuses_pickle_cache(monkeypatch, tmp_path: Path):
    rows = [(1900, 1900), (1900, 1901), (1902, 1902)]
    conn = _ContextConnection(rows)
    cache_path = tmp_path / "dating_state.pkl"

    monkeypatch.setattr(dating_conf, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(dating_conf, "_DATING_CACHE", cache_path)
    monkeypatch.setattr(dating_conf, "db_connect", lambda: conn)

    dating_conf._load_dating()

    assert cache_path.exists()
    assert dating_conf._dating_state["year_min"] == 1900
    assert dating_conf._dating_state["year_max"] == 1902
    assert dating_conf._dating_state["coverage"].tolist() == [2, 1, 1]
    assert conn.cursor_obj.executed_sql

    # Force reload. With cache present the DB must not be queried again.
    executed_before = list(conn.cursor_obj.executed_sql)
    dating_conf._dating_state = None
    dating_conf._load_dating()
    assert dating_conf._dating_state["coverage"].tolist() == [2, 1, 1]
    assert conn.cursor_obj.executed_sql == executed_before


def test_dating_score_rarity_reflects_coverage():
    _set_dating_state()
    # State: year_min=1900, year_max=1903; coverage=[10, 2, 1, 5]; log_N=log(10)
    # 1902 has coverage=1 → max IDF → score 1.0
    assert dating_conf._score_dating(1902, 1902) == pytest.approx(1.0)
    # 1900 has coverage=10 → IDF=0 → score 0.0
    assert dating_conf._score_dating(1900, 1900) == pytest.approx(0.0)
    # 1901 has coverage=2 → score strictly between
    medium = dating_conf._score_dating(1901, 1901)
    assert 0.0 < medium < 1.0
    # wide interval averaging over common and rare years → lower than the rarest single year
    wide = dating_conf._score_dating(1900, 1903)
    assert 0.0 < wide < 1.0
    assert wide < dating_conf._score_dating(1902, 1902)


def test_dating_score_partial_overlap_uses_original_interval_length():
    _set_dating_state()
    # Interval 1898–1901 clips to [1900, 1901] but divides by the original length=4.
    # idf[1900]=log(10/10)=0, idf[1901]=log(10/2)
    idf_1900 = math.log(10 / 10)
    idf_1901 = math.log(10 / 2)
    expected = (idf_1900 + idf_1901) / 4 / math.log(10)
    assert dating_conf._score_dating(1898, 1901) == pytest.approx(expected)


def test_dating_confidence_function_averages_both_valid_scores():
    _set_dating_state()
    lost = dating_conf._score_dating(1902, 1902)
    auction = dating_conf._score_dating(1901, 1901)
    assert dating_conf.confidence_function(1902, 1902, 1901, 1901) == pytest.approx(
        (lost + auction) / 2.0
    )


def test_dating_score_lazy_loads_when_state_is_none(monkeypatch):
    # _dating_state is None (reset by autouse fixture)
    monkeypatch.setattr(dating_conf, "_load_dating", _set_dating_state)
    result = dating_conf._score_dating(1902, 1902)
    assert result == pytest.approx(1.0)
    assert dating_conf._dating_state is not None


def _set_dimensions_state():
    dimensions_conf._dim_state = {
        "kde": _DummyKDE(
            {
                (10.0, 20.0): 0.50,
                (30.0, 40.0): 0.10,
                (100.0, 200.0): 0.01,
                (999.0, 999.0): 10.0,
            }
        ),
        "log_inv_max": math.log(1.0 / 0.01),
    }


def _expected_dimension_score(width: float, height: float) -> float:
    state = dimensions_conf._dim_state
    density = float(state["kde"](np.array([[width], [height]]))[0])
    log_inv = math.log(1.0 / max(density, 1e-12))
    return float(np.clip(log_inv / state["log_inv_max"], 0.0, 1.0))


def test_dimensions_confidence_function_scores_density_rarity_and_missing_sides():
    _set_dimensions_state()

    dense = _expected_dimension_score(10.0, 20.0)
    medium = _expected_dimension_score(30.0, 40.0)
    rare = _expected_dimension_score(100.0, 200.0)
    too_dense = _expected_dimension_score(999.0, 999.0)

    assert dense < medium < rare
    assert rare == pytest.approx(1.0)
    assert too_dense == pytest.approx(0.0)

    # confidence_function takes the two sides' *pre-scored* rarity confidences
    # (as computed by _score_dimensions per side, or 0.0 when a side is missing
    # its dimensions entirely -- see prepare_lost_row/prepare_auction_row in
    # main_matcher/metadata_matcher.py) and just averages them.
    assert dimensions_conf.confidence_function(dense, rare) == pytest.approx(
        (dense + rare) / 2.0
    )
    assert dimensions_conf.confidence_function(0.0, rare) == pytest.approx(
        rare / 2.0
    )
    assert dimensions_conf.confidence_function(rare, 0.0) == pytest.approx(
        rare / 2.0
    )
    assert dimensions_conf.confidence_function(0.0, 0.0) == 0.0


def test_dimensions_load_dimensions_builds_state_and_pickle_cache(monkeypatch, tmp_path: Path):
    rows = [(10.0, 20.0), (30.0, 40.0), (50.0, 60.0)]
    conn = _ContextConnection(rows)
    cache_path = tmp_path / "dim_state.pkl"

    monkeypatch.setattr(dimensions_conf, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(dimensions_conf, "_DIM_CACHE", cache_path)
    monkeypatch.setattr(dimensions_conf, "db_connect", lambda: conn)
    monkeypatch.setattr(dimensions_conf, "gaussian_kde", _FakeGaussianKDE)

    dimensions_conf._load_dimensions()

    assert cache_path.exists()
    assert "log_inv_min" not in dimensions_conf._dim_state
    assert dimensions_conf._dim_state["log_inv_max"] > 0
    assert conn.cursor_obj.executed_sql

    executed_before = list(conn.cursor_obj.executed_sql)
    dimensions_conf._dim_state = None
    dimensions_conf._load_dimensions()
    assert dimensions_conf._dim_state["log_inv_max"] > 0
    assert conn.cursor_obj.executed_sql == executed_before


def test_dimensions_score_lazy_loads_when_state_is_none(monkeypatch):
    # _dim_state is None (reset by autouse fixture)
    monkeypatch.setattr(dimensions_conf, "_load_dimensions", _set_dimensions_state)
    result = dimensions_conf._score_dimensions(10.0, 20.0)
    assert 0.0 <= result <= 1.0
    assert dimensions_conf._dim_state is not None


def _depth_ratio(name: str, hierarchy_type: str) -> float:
    hierarchy.load_hierarchies()
    if hierarchy_type == "material":
        paths = hierarchy.MATERIAL_PATHS_TO_ROOT
        path = hierarchy.get_path_to_root(name, "material")
    else:
        paths = hierarchy.TECHNIQUE_PATH_TO_ROOT
        path = hierarchy.get_path_to_root(name, "technique")

    max_depth = max(max(0, len(candidate_path) - 1) for candidate_path in paths.values())
    return (len(path) - 1) / max_depth


def test_technique_material_confidence_scores_actual_hierarchy_depth_specificity():
    hierarchy.load_hierarchies()

    material_score, technique_score = tm_conf.confidence_function(
        ["Pappelholz"],
        ["Pappelholz"],
        ["Kupferstich"],
        ["Kupferstich"],
    )

    assert material_score > 0.0
    assert technique_score > 0.0
    assert 0.0 <= material_score <= 1.0
    assert 0.0 <= technique_score <= 1.0


def test_technique_material_confidence_returns_zero_for_missing_or_root_sides():
    hierarchy.load_hierarchies()

    assert tm_conf.confidence_function(None, ["Pappelholz"],["Kupferstich"],["Kupferstich"],)[0] == 0.0
    assert tm_conf.confidence_function(["Pappelholz"], hierarchy.ROOT_NAME_MATERIAL, ["Kupferstich"],["Kupferstich"],)[0] == 0.0
    assert tm_conf.confidence_function(hierarchy.ROOT_NAME_MATERIAL,["Pappelholz"], ["Kupferstich"],["Kupferstich"],)[0] == 0.0
    assert tm_conf.confidence_function(["Pappelholz"], ["Pappelholz"],None,["Kupferstich"],)[1] == 0.0
    assert tm_conf.confidence_function(["Pappelholz"], ["Pappelholz"],[hierarchy.ROOT_NAME_TECHNIQUE],["Kupferstich"],)[1] == 0.0
    assert tm_conf.confidence_function(["Pappelholz"], ["Pappelholz"],["Kupferstich"],[hierarchy.ROOT_NAME_TECHNIQUE],)[1] == 0.0
