import os
import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from frontend import stats as stats_module
from frontend import stats_storage as stats_storage_module
from frontend.charts import make_bar_chart, make_line_chart
from frontend.stats_format import format_bytes, format_int
from frontend.stats_storage import image_file_metrics


class StatsHelperTests(unittest.TestCase):
    def test_line_chart_preserves_24_hour_points(self):
        labels = [f"{hour:02d}:00" for hour in range(24)]
        chart = make_line_chart(
            labels,
            [{"name": "Metadata", "values": range(24), "color": "#000"}],
        )

        self.assertEqual(chart["point_count"], 24)
        self.assertEqual(len(chart["series"][0]["data"]), 24)
        self.assertTrue(chart["show_points"])
        self.assertTrue(chart["series"][0]["points"])

    def test_bar_chart_uses_largest_value_as_full_width(self):
        bars = make_bar_chart(
            [
                {"label": "A", "value": 2, "color": "#000"},
                {"label": "B", "value": 4, "color": "#111"},
            ]
        )

        self.assertEqual(bars[0]["percent"], 50)
        self.assertEqual(bars[1]["percent"], 100)

    def test_formatters(self):
        self.assertEqual(format_int(1234567), "1.234.567")
        self.assertEqual(format_bytes(1536), "1.5 KB")

    def test_image_file_metrics_counts_missing_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "image.jpg"
            file_path.write_bytes(b"abcde")
            previous_root = os.environ.get("SMARTMATCH_IMAGES_DIR")
            os.environ["SMARTMATCH_IMAGES_DIR"] = tmp_dir
            try:
                metrics = image_file_metrics([str(file_path), "missing.jpg"])
            finally:
                if previous_root is None:
                    os.environ.pop("SMARTMATCH_IMAGES_DIR", None)
                else:
                    os.environ["SMARTMATCH_IMAGES_DIR"] = previous_root

        self.assertEqual(metrics["count"], 2)
        self.assertEqual(metrics["disk_bytes"], 5)
        self.assertEqual(metrics["missing_count"], 1)

    def test_image_file_metrics_returns_placeholder_while_refreshing_async(self):
        with stats_storage_module._SCAN_CACHE_LOCK:
            stats_storage_module._SCAN_CACHE.update(
                {
                    "signature": None,
                    "disk_bytes": None,
                    "missing_count": None,
                    "expires_at": 0.0,
                    "refreshing": False,
                }
            )

        with patch("frontend.stats_storage.Thread") as thread_class:
            metrics = image_file_metrics(["image.jpg"], refresh_async=True)

        self.assertEqual(metrics["disk_label"], "xx GB")
        self.assertEqual(metrics["missing_count"], 0)
        thread_class.assert_called_once()
        thread_class.return_value.start.assert_called_once()
        with stats_storage_module._SCAN_CACHE_LOCK:
            stats_storage_module._SCAN_CACHE.update(
                {
                    "signature": None,
                    "disk_bytes": None,
                    "missing_count": None,
                    "expires_at": 0.0,
                    "refreshing": False,
                }
            )

    def test_image_file_metrics_keeps_stale_value_while_refreshing(self):
        with stats_storage_module._SCAN_CACHE_LOCK:
            stats_storage_module._SCAN_CACHE.update(
                {
                    "signature": ("old-root", ("old.jpg",)),
                    "disk_bytes": 1536,
                    "missing_count": 2,
                    "expires_at": 0.0,
                    "refreshing": False,
                }
            )

        with patch("frontend.stats_storage.Thread") as thread_class:
            metrics = image_file_metrics(["new.jpg"], refresh_async=True)

        self.assertEqual(metrics["disk_bytes"], 1536)
        self.assertEqual(metrics["disk_label"], "1.5 KB")
        self.assertEqual(metrics["missing_count"], 2)
        thread_class.assert_called_once()
        thread_class.return_value.start.assert_called_once()
        with stats_storage_module._SCAN_CACHE_LOCK:
            self.assertEqual(stats_storage_module._SCAN_CACHE["disk_bytes"], 1536)
            stats_storage_module._SCAN_CACHE.update(
                {
                    "signature": None,
                    "disk_bytes": None,
                    "missing_count": None,
                    "expires_at": 0.0,
                    "refreshing": False,
                }
            )

    def test_image_file_metrics_does_not_refresh_before_ttl_expires(self):
        with stats_storage_module._SCAN_CACHE_LOCK:
            stats_storage_module._SCAN_CACHE.update(
                {
                    "signature": ("root", ("image.jpg",)),
                    "disk_bytes": 2048,
                    "missing_count": 0,
                    "expires_at": time.monotonic() + 60,
                    "refreshing": False,
                }
            )

        with patch("frontend.stats_storage.Thread") as thread_class:
            metrics = image_file_metrics(["image.jpg"], refresh_async=True)

        self.assertEqual(metrics["disk_label"], "2 KB")
        thread_class.assert_not_called()
        with stats_storage_module._SCAN_CACHE_LOCK:
            stats_storage_module._SCAN_CACHE.update(
                {
                    "signature": None,
                    "disk_bytes": None,
                    "missing_count": None,
                    "expires_at": 0.0,
                    "refreshing": False,
                }
            )

    def test_dashboard_stats_includes_auction_artwork_total(self):
        captured_statements = []

        def fake_rows(engine, statement, **params):
            captured_statements.append(statement)
            if "FROM lost_artwork" in statement and "FROM auction_artwork" in statement:
                return [
                    {"lost_artwork_count": 1234, "auction_artwork_count": 5678}
                ]
            self.fail(f"Unexpected rows query: {statement}")

        engine = object()
        stats_module.clear_dashboard_stats_cache()
        with self.assertLogs("frontend.stats", level="WARNING"):
            with patch("frontend.stats._rows", side_effect=fake_rows):
                with patch("frontend.stats._auction_artworks_over_time", return_value={}):
                    with patch("frontend.stats._match_throughput_24h", return_value={}):
                        with patch("frontend.stats._pipeline_throughput_24h", return_value={}):
                            with patch("frontend.stats._image_file_metrics", return_value={}):
                                with patch("frontend.stats._match_categories", return_value=[]):
                                    stats = stats_module.get_dashboard_stats(engine)

        self.assertEqual(len(captured_statements), 1)
        self.assertEqual(stats["lost_artwork_count"], 1234)
        self.assertEqual(stats["lost_artwork_label"], "1.234")
        self.assertEqual(stats["auction_artwork_count"], 5678)
        self.assertEqual(stats["auction_artwork_label"], "5.678")

    def test_dashboard_stats_logs_one_timing_message_for_metric_aggregation(self):
        def fake_rows(engine, statement, **params):
            self.assertIs(engine, test_engine)
            self.assertEqual(params, {})
            if statement == stats_module.stats_sql.DASHBOARD_COUNTS_SQL:
                return [{"lost_artwork_count": 1, "auction_artwork_count": 2}]
            if statement == stats_module.stats_sql.AUCTION_ARTWORKS_OVER_TIME_SQL:
                return []
            if statement == stats_module.stats_sql.MATCH_THROUGHPUT_24H_SQL:
                return []
            if statement == stats_module.stats_sql.PIPELINE_THROUGHPUT_24H_SQL:
                return []
            if statement == stats_module.stats_sql.IMAGE_FILE_PATHS_SQL:
                return [{"file_path": "missing.jpg"}]
            if statement == stats_module.stats_sql.MATCH_CATEGORIES_SQL:
                return [
                    {
                        "new_total": 0,
                        "bookmarked_total": 0,
                        "accepted_total": 0,
                        "discarded_total": 0,
                    }
                ]
            self.fail(f"Unexpected rows query: {statement}")

        test_engine = object()
        with self.assertLogs("frontend.stats", level="WARNING") as logs:
            with patch("frontend.stats._rows", side_effect=fake_rows):
                with patch("frontend.stats.image_file_metrics", return_value={}):
                    stats_module._collect_dashboard_stats(test_engine)

        self.assertEqual(len(logs.output), 1)
        message = logs.output[0]
        self.assertIn("/stats metric aggregation timings:", message)
        for label in (
            "artwork totals database query",
            "auction artwork processing history database query",
            "match score throughput database query",
            "auction pipeline throughput database query",
            "image file path database query",
            "image filesystem size scan",
            "match review status database query",
            "total stats aggregation",
        ):
            self.assertIn(f"{label}:", message)

    def test_dashboard_stats_uses_short_lived_cache(self):
        calls = []

        def fake_collect(engine):
            calls.append(engine)
            return {"call_count": len(calls)}

        engine = object()
        stats_module.clear_dashboard_stats_cache()
        with patch.dict(os.environ, {"SMARTMATCH_STATS_CACHE_TTL_SECONDS": "60"}):
            with patch("frontend.stats._collect_dashboard_stats", side_effect=fake_collect):
                first = stats_module.get_dashboard_stats(engine)
                second = stats_module.get_dashboard_stats(engine)

        stats_module.clear_dashboard_stats_cache()
        self.assertEqual(first, {"call_count": 1})
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)

    def test_auction_over_time_counts_total_artworks_by_created_at(self):
        captured = {}
        rows = [
            {
                "bucket": date(2026, 6, 9),
                "total": 2,
                "image_matching_processed_total": 1,
                "metadata_matching_processed_total": 2,
                "metadata_extraction_processed_total": 2,
            },
            {
                "bucket": date(2026, 6, 10),
                "total": 5,
                "image_matching_processed_total": 3,
                "metadata_matching_processed_total": 4,
                "metadata_extraction_processed_total": 5,
            },
            {
                "bucket": date(2026, 6, 11),
                "total": 9,
                "image_matching_processed_total": 4,
                "metadata_matching_processed_total": 5,
                "metadata_extraction_processed_total": 6,
            },
        ]

        def fake_rows(engine, statement, **params):
            captured["engine"] = engine
            captured["statement"] = statement
            captured["params"] = params
            return rows

        engine = object()
        with patch("frontend.stats._rows", side_effect=fake_rows):
            auction_over_time = stats_module._auction_artworks_over_time(engine)

        self.assertIs(captured["engine"], engine)
        self.assertIn("event_counts AS", captured["statement"])
        self.assertIn("FROM auction_artwork", captured["statement"])
        self.assertNotIn("auction_date", captured["statement"])
        self.assertNotIn("LEAST(", captured["statement"])
        self.assertNotIn("GREATEST(", captured["statement"])
        self.assertIn("aa.created_at::date", captured["statement"])
        self.assertIn("'total' AS kind", captured["statement"])
        self.assertNotIn("'unprocessed' AS kind", captured["statement"])
        self.assertNotIn("is_image_matching_processed = false", captured["statement"])
        self.assertNotIn("is_metadata_matching_processed = false", captured["statement"])
        self.assertIn("SUM(daily_count) FILTER (WHERE kind = 'total')", captured["statement"])
        self.assertIn("SUM(daily_total) OVER", captured["statement"])
        self.assertNotIn("unprocessed_total", captured["statement"])
        self.assertIn("image_matching_processed_total", captured["statement"])
        self.assertIn("metadata_matching_processed_total", captured["statement"])
        self.assertIn("metadata_extraction_processed_total", captured["statement"])
        self.assertNotIn("artwork_totals", captured["statement"])
        self.assertIn("is_image_matching_processed_at", captured["statement"])
        self.assertIn("is_metadata_matching_processed_at", captured["statement"])
        self.assertIn("is_metadata_extraction_processed_at", captured["statement"])
        self.assertIn("ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW", captured["statement"])
        self.assertEqual(captured["params"], {})
        self.assertEqual(auction_over_time["chart"]["series"][0]["values"], [2, 5, 9])
        self.assertEqual(
            auction_over_time["chart"]["series"][0]["name"],
            "Auktionswerke gesamt",
        )
        self.assertEqual(auction_over_time["chart"]["series"][0]["legend_value_label"], "9")
        self.assertEqual(
            auction_over_time["chart"]["series"][1]["name"],
            "Bild-Matching verarbeitet",
        )
        self.assertEqual(auction_over_time["chart"]["series"][1]["values"], [1, 3, 4])
        self.assertEqual(auction_over_time["chart"]["series"][1]["legend_value_label"], "4")
        self.assertEqual(
            auction_over_time["chart"]["series"][2]["name"],
            "Metadata-Matching verarbeitet",
        )
        self.assertEqual(
            auction_over_time["chart"]["series"][3]["name"],
            "Metadata-Extraktion verarbeitet",
        )

    def test_throughput_includes_image_artwork_and_metadata_matches(self):
        captured = {}
        rows = [
            {
                "bucket": datetime(2026, 6, 11, 11, tzinfo=timezone.utc),
                "metadata_total": 3,
                "image_total": 10,
            }
        ]

        def fake_rows(engine, statement, **params):
            captured["engine"] = engine
            captured["statement"] = statement
            captured["params"] = params
            return rows

        engine = object()
        with patch("frontend.stats._rows", side_effect=fake_rows):
            throughput = stats_module._match_throughput_24h(engine)

        self.assertIs(captured["engine"], engine)
        self.assertIn("metadata_counts AS", captured["statement"])
        self.assertIn("image_counts AS", captured["statement"])
        self.assertIn("FROM match_score ms", captured["statement"])
        self.assertIn("ms.metadata_match_date", captured["statement"])
        self.assertIn("ms.image_match_date", captured["statement"])
        self.assertNotIn("UNION ALL", captured["statement"])
        self.assertEqual(captured["params"], {})
        self.assertEqual(throughput["chart"]["series"][0]["values"], [3])
        self.assertEqual(throughput["chart"]["series"][1]["values"], [10])
        self.assertEqual(
            throughput["chart"]["x_labels"][0]["time_iso"],
            "2026-06-11T11:00:00Z",
        )
        self.assertEqual(throughput["chart"]["x_labels"][0]["time_format"], "time")
        self.assertEqual(
            throughput["chart"]["series"][0]["data"][0]["time_iso"],
            "2026-06-11T11:00:00Z",
        )

    def test_pipeline_throughput_counts_auction_artwork_processing_timestamps(self):
        captured = {}
        rows = [
            {
                "bucket": datetime(2026, 6, 11, 11, tzinfo=timezone.utc),
                "image_matching_total": 5,
                "metadata_matching_total": 7,
                "metadata_extraction_total": 11,
            }
        ]

        def fake_rows(engine, statement, **params):
            captured["engine"] = engine
            captured["statement"] = statement
            captured["params"] = params
            return rows

        engine = object()
        with patch("frontend.stats._rows", side_effect=fake_rows):
            throughput = stats_module._pipeline_throughput_24h(engine)

        self.assertIs(captured["engine"], engine)
        self.assertIn("processed_events AS", captured["statement"])
        self.assertIn("pipeline_counts AS", captured["statement"])
        self.assertIn("FROM auction_artwork", captured["statement"])
        self.assertIn("CROSS JOIN LATERAL", captured["statement"])
        self.assertIn("is_image_matching_processed_at", captured["statement"])
        self.assertIn("is_metadata_matching_processed_at", captured["statement"])
        self.assertIn("is_metadata_extraction_processed_at", captured["statement"])
        self.assertEqual(captured["params"], {})
        self.assertEqual(throughput["chart"]["series"][0]["values"], [5])
        self.assertEqual(throughput["chart"]["series"][1]["values"], [7])
        self.assertEqual(throughput["chart"]["series"][2]["values"], [11])

    def test_match_categories_counts_match_score_review_state(self):
        captured = {}
        rows = [
            {
                "new_total": 4,
                "bookmarked_total": 2,
                "accepted_total": 1,
                "discarded_total": 3,
            }
        ]

        def fake_rows(engine, statement, **params):
            captured["engine"] = engine
            captured["statement"] = statement
            captured["params"] = params
            return rows

        engine = object()
        with patch("frontend.stats._rows", side_effect=fake_rows):
            categories = stats_module._match_categories(engine)

        self.assertIs(captured["engine"], engine)
        self.assertNotIn("WITH match_categories AS", captured["statement"])
        self.assertIn("FROM match_score", captured["statement"])
        self.assertNotIn("FROM artwork_match", captured["statement"])
        self.assertNotIn("UNION ALL", captured["statement"])
        self.assertEqual(captured["params"], {})
        self.assertEqual(categories[0]["value"], 4)


if __name__ == "__main__":
    unittest.main()
