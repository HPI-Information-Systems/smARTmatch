import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from frontend import app
from frontend.sql import matches as match_sql


DIRECTORY_COUNTS = {
    "all": 0,
    "new": 0,
    "bookmarked": 0,
    "accepted": 0,
    "discarded": 0,
}


class _FakeRowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _FakeOneResult:
    def __init__(self, row):
        self._row = row

    def one(self):
        return self._row


def _page_row(source="metadata", score=0.9):
    match_date = datetime(2026, 6, 12, tzinfo=timezone.utc)
    return SimpleNamespace(
        match_id=f"{source}-match-id",
        lost_artwork_id="lost-id",
        auction_artwork_id="auction-id",
        final_score=score,
        image_similarity=None,
        metadata_similarity=score,
        rating=0,
        bookmarked=False,
        match_date=match_date,
        visualization={},
        best_image_file_id=None,
        metadata_match_id=f"{source}-match-id",
        metadata_confidence_score=0.8,
        metadata_event_date=match_date,
        metadata_matching_program="program-id",
        metadata_matching_program_name="metadata matching",
        metadata_matching_program_version="1.0",
        title_sim=score,
        artist_sim=None,
        dating_sim=None,
        dimensions_sim=None,
        material_sim=None,
        technique_sim=None,
        lost_title="Lost Title",
        lost_dating="1900",
        lost_width=10,
        lost_height=20,
        lost_width_frame=None,
        lost_height_frame=None,
        lost_depth=None,
        lost_diameter=None,
        lost_material="Oil",
        lost_technique="Canvas",
        lost_artist_names=["Lost Artist"],
        lost_image_file_ids=[],
        lost_image_paths=[],
        auction_title="Auction Title",
        auction_artist_raw_data="Auction Artist",
        auction_artist_full_name="Auction Artist",
        auction_artist_name=None,
        auction_dating="1901",
        auction_width=11,
        auction_height=21,
        auction_width_frame=None,
        auction_height_frame=None,
        auction_dimensions_raw_data=None,
        auction_material="Oil",
        auction_technique="Canvas",
        auction_image_file_ids=[],
        auction_image_paths=[],
    )


class MatchListQueryTests(unittest.TestCase):
    def test_get_matches_page_uses_sql_grouping_and_pagination(self):
        with patch.object(
            app.session,
            "execute",
            side_effect=[_FakeScalarResult(1), _FakeRowsResult([_page_row()])],
        ) as execute:
            match_page = app.get_matches_page(image_weight=0)

        self.assertEqual(match_page.total_matches, 1)
        self.assertEqual(len(match_page.matches), 1)
        self.assertEqual(match_page.matches[0].metadata_similarity, 0.9)
        self.assertIsNone(match_page.matches[0].image_similarity)
        self.assertEqual(execute.call_count, 2)
        count_sql = str(execute.call_args_list[0].args[0])
        page_sql = str(execute.call_args_list[1].args[0])
        self.assertIn("FROM match_score ms", page_sql)
        self.assertNotIn("UNION ALL", page_sql)
        self.assertIn("LIMIT :limit OFFSET :offset", page_sql)
        self.assertLess(
            page_sql.index("LIMIT :limit OFFSET :offset"),
            page_sql.index("JOIN lost_artwork l"),
        )
        self.assertIn("source_lost.institution_classification", count_sql)
        self.assertEqual(count_sql.count("SELECT count(*)"), 1)

    def test_match_filter_params_are_cast_for_postgres_type_inference(self):
        count_sql = match_sql.match_count_sql(False)
        search_sql = match_sql.match_count_sql(True)
        neighbors_sql = match_sql.match_neighbors_sql("similarity", False)

        self.assertIn("CAST(:apply_filters AS boolean) = false", count_sql)
        self.assertIn("CAST(:rating AS text) IS NULL", count_sql)
        self.assertIn("CAST(:bookmarked AS text) IS NULL", count_sql)
        self.assertIn("CAST(:source_filter AS text) = 'all'", count_sql)
        self.assertIn(
            "institution_classification = CAST(:source_filter AS text)", count_sql
        )
        self.assertNotIn("Museum A", count_sql)
        self.assertIn("ILIKE CAST(:search_like AS text)", search_sql)
        self.assertIn("og.match_id = CAST(:current_match_id AS text)", neighbors_sql)

    def test_directory_counts_use_one_grouped_sql_request(self):
        row = SimpleNamespace(
            all_count=5,
            new_count=2,
            bookmarked_count=1,
            accepted_count=1,
            discarded_count=1,
        )
        with patch.object(
            app.session, "execute", return_value=_FakeOneResult(row)
        ) as execute:
            counts = app.get_directory_counts()

        self.assertEqual(counts["new"], 2)
        execute.assert_called_once()
        sql = str(execute.call_args.args[0])
        self.assertNotIn("match_categories AS", sql)
        self.assertIn("count(*) FILTER", sql)
        self.assertIn("FROM match_score", sql)
        self.assertNotIn("UNION ALL", sql)

    def test_previous_next_match_uses_sql_window_neighbors(self):
        previous_row = _page_row("previous", 0.8)
        previous_row.neighbor_position = -1
        next_row = _page_row("next", 0.7)
        next_row.neighbor_position = 1
        with patch.object(
            app.session,
            "execute",
            return_value=_FakeRowsResult([previous_row, next_row]),
        ) as execute:
            previous_match, next_match = app.get_previous_and_next_match(
                "00000000-0000-0000-0000-000000000001"
            )

        self.assertEqual(previous_match.match_id, "previous-match-id")
        self.assertEqual(next_match.match_id, "next-match-id")
        execute.assert_called_once()
        sql = str(execute.call_args.args[0])
        self.assertIn("lag(fg.match_id) OVER", sql)
        self.assertIn("lead(fg.match_id) OVER", sql)
        self.assertIn("current_neighbors AS", sql)

    def test_source_options_hide_for_zero_or_one_database_classification(self):
        cases = [
            [],
            [SimpleNamespace(institution_classification=None)],
            [SimpleNamespace(institution_classification="Museum A")],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                with patch.object(
                    app.session, "execute", return_value=_FakeRowsResult(rows)
                ):
                    self.assertEqual(app.get_new_match_source_filter_options(), [])

    def test_source_options_are_discovered_generically_from_the_database(self):
        rows = [
            SimpleNamespace(institution_classification=None),
            SimpleNamespace(institution_classification="Museum A"),
            SimpleNamespace(institution_classification="Museum B"),
        ]
        with patch.object(
            app.session, "execute", return_value=_FakeRowsResult(rows)
        ) as execute:
            options = app.get_new_match_source_filter_options()

        self.assertEqual(
            options,
            [
                {"value": "Museum A", "label": "Museum A"},
                {"value": "Museum B", "label": "Museum B"},
            ],
        )
        sql = str(execute.call_args.args[0])
        self.assertIn("FROM lost_artwork", sql)
        self.assertIn("institution_classification", sql)
        self.assertNotIn("match_score", sql)
        self.assertNotIn("Museum A", sql)
        self.assertNotIn("Museum B", sql)

    def test_best_auction_image_is_ordered_first_for_gallery(self):
        row = _page_row()
        row.best_image_file_id = 22
        row.auction_image_file_ids = [11, 22, 33]
        row.auction_image_paths = ["auction-a.jpg", "auction-best.jpg", "auction-c.jpg"]

        match = app._page_row_combined_match(row)

        self.assertEqual(
            [image.image_file_id for image in match.auction_artwork.image_files],
            [22, 11, 33],
        )
        self.assertEqual(
            match.auction_artwork.image_paths,
            ["auction-best.jpg", "auction-a.jpg", "auction-c.jpg"],
        )


class MatchListTemplateTests(unittest.TestCase):
    def setUp(self):
        app.app.config["TESTING"] = True
        self.client = app.app.test_client()

    def render_list(self, path, matches=None, source_options=None):
        matches = matches or []
        source_options = source_options or []
        match_page = app.MatchPage(
            matches=matches,
            total_matches=len(matches),
            page=1,
            per_page=20,
            max_page=1 if matches else 0,
        )
        with patch.object(app, "get_matches_page", return_value=match_page):
            with patch.object(
                app, "get_directory_counts", return_value=DIRECTORY_COUNTS
            ):
                with patch.object(
                    app,
                    "get_new_match_source_filter_options",
                    return_value=source_options,
                ):
                    return self.client.get(path)

    def render_detail(self, match):
        with patch.object(app, "get_match_by_id", return_value=match):
            with patch.object(
                app, "get_previous_and_next_match", return_value=(None, None)
            ):
                return self.client.get(f"/match/{match.match_id}")

    def example_match(self):
        lost_artwork = SimpleNamespace(
            lost_artwork_id="lost-artwork-1",
            title="Lost Title",
            artists=[SimpleNamespace(complete_name="Lost Artist")],
            dating="1900",
            width=12,
            height=34,
            width_frame=None,
            height_frame=None,
            depth=None,
            diameter=None,
            material_labels=["Oil"],
            technique_labels=["Canvas"],
            image_paths=[],
            image_files=[],
        )
        auction_artwork = SimpleNamespace(
            auction_artwork_id="auction-artwork-1",
            title="Auction Title",
            artist_raw_data="Auction Artist",
            artist_full_name="Auction Artist",
            dating="1901",
            width=56.5,
            height=78,
            width_frame=None,
            height_frame=None,
            dimensions_raw_data=None,
            material_labels=["Oil"],
            technique_labels=["Canvas"],
            image_paths=[],
            image_files=[],
        )
        return SimpleNamespace(
            match_id="match-1",
            lost_artwork_id="lost-artwork-1",
            auction_artwork_id="auction-artwork-1",
            lost_artwork=lost_artwork,
            auction_artwork=auction_artwork,
            final_score=0.65,
            image_similarity=0.8,
            metadata_similarity=0.5,
            rating=0,
            bookmarked=False,
            visualization={},
            best_image_file_id=None,
        )

    def test_all_matches_category_is_hidden_without_search(self):
        response = self.render_list("/list")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn("Alle Matches", html)
        self.assertIn("Unbewertet", html)

    def test_all_matches_category_is_shown_for_search(self):
        response = self.render_list("/list?search=monet")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Alle Matches", html)
        self.assertIn("Suchergebnisse", html)

    def test_default_list_route_sorts_unrated_by_similarity(self):
        match_page = app.MatchPage(
            matches=[],
            total_matches=0,
            page=1,
            per_page=20,
            max_page=0,
        )
        with patch.object(app, "get_matches_page", return_value=match_page) as get_page:
            with patch.object(
                app, "get_directory_counts", return_value=DIRECTORY_COUNTS
            ):
                with patch.object(
                    app, "get_new_match_source_filter_options", return_value=[]
                ):
                    response = self.client.get("/list")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_page.call_args.kwargs["rating"], "unrated")
        self.assertEqual(get_page.call_args.kwargs["sort"], "similarity")

    def test_reviewed_category_defaults_sort_by_newest(self):
        cases = [
            "/list?rating=all&bookmarked=true",
            "/list?rating=accepted&bookmarked=all",
            "/list?rating=discarded&bookmarked=all",
        ]
        for path in cases:
            with self.subTest(path=path):
                match_page = app.MatchPage(
                    matches=[],
                    total_matches=0,
                    page=1,
                    per_page=20,
                    max_page=0,
                )
                with patch.object(
                    app, "get_matches_page", return_value=match_page
                ) as get_page:
                    with patch.object(
                        app, "get_directory_counts", return_value=DIRECTORY_COUNTS
                    ):
                        response = self.client.get(path)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(get_page.call_args.kwargs["sort"], "newest")

    def test_source_filter_is_hidden_with_fewer_than_two_classifications(self):
        response = self.render_list("/list")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('id="source-filter"', response.get_data(as_text=True))

    def test_institution_filter_preserves_dynamic_selection(self):
        options = [{"value": "Museum A", "label": "Museum A"}]
        response = self.render_list(
            "/list?source_filter=Museum%20A",
            matches=[self.example_match()],
            source_options=options,
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="source-filter"', html)
        self.assertIn(">Institution</label>", html)
        self.assertIn('value="Museum A" selected', html)
        self.assertIn('name="source_filter" value="Museum A"', html)
        self.assertIn("source_filter=Museum+A", html)
        self.assertIn(">Museum A</option>", html)
        self.assertNotIn("— Museum A", html)

    def test_source_filter_defaults_to_all_institutions(self):
        match_page = app.MatchPage([], 0, 1, 20, 0)
        options = [{"value": "Museum A", "label": "Museum A"}]
        with patch.object(app, "get_matches_page", return_value=match_page) as get_page:
            with patch.object(
                app, "get_directory_counts", return_value=DIRECTORY_COUNTS
            ):
                with patch.object(
                    app, "get_new_match_source_filter_options", return_value=options
                ):
                    response = self.client.get("/list")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_page.call_args.kwargs["source_filter"], "all")
        html = response.get_data(as_text=True)
        self.assertIn('value="all" selected', html)
        self.assertIn("Alle Institutionen", html)
        self.assertNotIn("Alle Klassifikationen", html)

    def test_source_filter_is_applied_to_the_new_match_query(self):
        match_page = app.MatchPage([], 0, 1, 20, 0)
        options = [{"value": "Museum A", "label": "Museum A"}]
        with patch.object(app, "get_matches_page", return_value=match_page) as get_page:
            with patch.object(
                app, "get_directory_counts", return_value=DIRECTORY_COUNTS
            ):
                with patch.object(
                    app, "get_new_match_source_filter_options", return_value=options
                ):
                    response = self.client.get("/list?source_filter=Museum%20A")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_page.call_args.kwargs["source_filter"], "Museum A")

    def test_unavailable_source_filter_falls_back_to_all(self):
        match_page = app.MatchPage([], 0, 1, 20, 0)
        options = [{"value": "Museum A", "label": "Museum A"}]
        with patch.object(app, "get_matches_page", return_value=match_page) as get_page:
            with patch.object(
                app, "get_directory_counts", return_value=DIRECTORY_COUNTS
            ):
                with patch.object(
                    app, "get_new_match_source_filter_options", return_value=options
                ):
                    response = self.client.get(
                        "/list?source_filter=Unknown%20Museum"
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_page.call_args.kwargs["source_filter"], "all")
        self.assertIn(
            'value="all" selected', response.get_data(as_text=True)
        )

    def test_source_filter_is_hidden_outside_new_matches(self):
        options = [{"value": "Museum A", "label": "Museum A"}]
        response = self.render_list(
            "/list?rating=accepted&bookmarked=all", source_options=options
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('id="source-filter"', response.get_data(as_text=True))

    def test_category_sidebar_links_use_default_sorts(self):
        response = self.render_list("/list")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("rating=unrated&amp;sort=similarity", html)
        self.assertIn("rating=all&amp;sort=newest&amp;bookmarked=true", html)
        self.assertIn("rating=accepted&amp;sort=newest", html)
        self.assertIn("rating=discarded&amp;sort=newest", html)

    def test_match_overview_contains_dimensions(self):
        response = self.render_list("/list", matches=[self.example_match()])

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Maße", html)
        self.assertIn("12 x 34 cm", html)
        self.assertIn("56,5 x 78 cm", html)

    def test_match_overview_prefers_extracted_artist_full_name(self):
        match = self.example_match()
        match.auction_artwork.artist_raw_data = "Raw Auction Artist"
        match.auction_artwork.artist_full_name = "Extracted Auction Artist"
        response = self.render_list("/list", matches=[match])

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Extracted Auction Artist", html)
        self.assertNotIn("Raw Auction Artist", html)

    def test_match_overview_uses_best_auction_image_file(self):
        match = self.example_match()
        match.best_image_file_id = 22
        match.auction_artwork.image_paths = ["auction-other.jpg", "auction-best.jpg"]
        response = self.render_list("/list", matches=[match])

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('/db_image/file/22', html)
        self.assertNotIn('/db_image/auction/auction-artwork-1', html)

    def test_match_detail_prefers_extracted_artist_full_name(self):
        match = self.example_match()
        match.auction_artwork.artist_raw_data = "Raw Detail Artist"
        match.auction_artwork.artist_full_name = "Extracted Detail Artist"
        response = self.render_detail(match)

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Extracted Detail Artist", html)
        self.assertNotIn("Raw Detail Artist", html)

    def test_combined_score_uses_weighted_component_bars(self):
        response = self.render_list("/list?image_weight=25", matches=[self.example_match()])

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("score-progress-combined-image", html)
        self.assertIn("score-progress-combined-metadata", html)
        self.assertIn("width: 20.00%", html)
        self.assertIn("width: 37.50%", html)

    def test_match_detail_renders_auction_gallery_thumbnails(self):
        match = self.example_match()
        match.auction_artwork.image_paths = ["auction-best.jpg", "auction-other.jpg"]
        match.auction_artwork.image_files = [
            SimpleNamespace(image_file_id=22, file_path="auction-best.jpg"),
            SimpleNamespace(image_file_id=11, file_path="auction-other.jpg"),
        ]

        response = self.render_detail(match)

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("auction-gallery-main", html)
        self.assertIn("/db_image/file/22", html)
        self.assertIn("/db_image/file/11", html)
        self.assertIn('data-bs-slide-to="1"', html)

    def test_match_detail_renders_keypoints_on_canvas_not_prerendered_plot(self):
        match = self.example_match()
        match.lost_artwork.image_paths = ["lost.jpg"]
        match.lost_artwork.image_files = [
            SimpleNamespace(image_file_id=33, file_path="lost.jpg")
        ]
        match.auction_artwork.image_paths = ["auction.jpg"]
        match.auction_artwork.image_files = [
            SimpleNamespace(image_file_id=22, file_path="auction.jpg")
        ]
        match.visualization = {
            "image_matching": {
                "best_match": {
                    "lost_image_file_id": 33,
                    "auction_image_file_id": 22,
                    "keypoint_matches": {
                        "coordinate_space": "image_pixels",
                        "match_count": 1,
                        "matches": [
                            {
                                "lost": {"x": 10.0, "y": 20.0},
                                "auction": {"x": 30.0, "y": 40.0},
                                "score": 0.9,
                            }
                        ],
                    },
                }
            }
        }

        response = self.render_detail(match)

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("data-keypoint-viewer", html)
        self.assertIn("keypoint-match-canvas", html)
        self.assertIn("/db_image/file/33", html)
        self.assertIn("/db_image/file/22", html)
        self.assertNotIn("/db_image/plot/", html)


if __name__ == "__main__":
    unittest.main()
