"""Database-backed statistics for the SmartMatch frontend dashboard."""

from __future__ import annotations

import logging
import os
import time
from contextlib import nullcontext
from datetime import date, datetime, time as datetime_time, timezone
from threading import Lock

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .charts import make_bar_chart, make_line_chart
from .stats_format import format_int
from .stats_storage import image_file_metrics
from .stats_timing import StatsTimingCollector
from .sql import stats as stats_sql


_LOGGER = logging.getLogger(__name__)
_CACHE_LOCK = Lock()
_DASHBOARD_STATS_CACHE = {"key": None, "expires_at": 0.0, "value": None}


def _cache_ttl_seconds():
    value = os.getenv("SMARTMATCH_STATS_CACHE_TTL_SECONDS", "60")
    try:
        seconds = int(value)
    except ValueError as exc:
        raise ValueError(
            "Environment variable SMARTMATCH_STATS_CACHE_TTL_SECONDS must be an integer"
        ) from exc
    return max(0, seconds)


def _cache_key(engine):
    url = getattr(engine, "url", None)
    return str(url) if url is not None else str(id(engine))


def clear_dashboard_stats_cache():
    """Clear the in-process /stats dashboard cache."""
    with _CACHE_LOCK:
        _DASHBOARD_STATS_CACHE.update({"key": None, "expires_at": 0.0, "value": None})


def _connection(connectable):
    if isinstance(connectable, Engine):
        return connectable.connect()
    return nullcontext(connectable)


def _rows(connectable, statement, **params):
    with _connection(connectable) as connection:
        return connection.execute(text(statement), params).mappings().all()


def _scalar(connectable, statement, **params):
    with _connection(connectable) as connection:
        return connection.execute(text(statement), params).scalar_one()


def _timed_rows(connectable, timings, label, statement, **params):
    if timings is None:
        return _rows(connectable, statement, **params)
    with timings.measure(label):
        return _rows(connectable, statement, **params)


def _log_dashboard_stats_timing(timings):
    _LOGGER.warning(
        "/stats metric aggregation timings: %s", timings.format_message()
    )


def _js_time_iso(value):
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, date):
        timestamp = datetime.combine(value, datetime_time.min, tzinfo=timezone.utc)
    else:
        return None

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _chart_time_labels(rows, label_format):
    return [
        {"iso": _js_time_iso(row["bucket"]), "format": label_format}
        for row in rows
    ]


def _dashboard_counts(connectable, timings=None):
    row = _timed_rows(
        connectable,
        timings,
        "artwork totals database query",
        stats_sql.DASHBOARD_COUNTS_SQL,
    )[0]
    return row["lost_artwork_count"], row["auction_artwork_count"]


def _auction_artworks_over_time(engine, timings=None):
    rows = _timed_rows(
        engine,
        timings,
        "auction artwork processing history database query",
        stats_sql.AUCTION_ARTWORKS_OVER_TIME_SQL,
    )
    labels = [row["bucket"].strftime("%d.%m.%y") for row in rows]
    time_labels = _chart_time_labels(rows, "date")
    series = [
        {
            "name": "Auktionswerke gesamt",
            "values": [row["total"] for row in rows],
            "color": "#002851",
            "legend_value_label": format_int(rows[-1]["total"] if rows else 0),
        },
        {
            "name": "Bild-Matching verarbeitet",
            "values": [row["image_matching_processed_total"] for row in rows],
            "color": "#18a558",
            "legend_value_label": format_int(
                rows[-1]["image_matching_processed_total"] if rows else 0
            ),
        },
        {
            "name": "Metadata-Matching verarbeitet",
            "values": [row["metadata_matching_processed_total"] for row in rows],
            "color": "#e5532f",
            "legend_value_label": format_int(
                rows[-1]["metadata_matching_processed_total"] if rows else 0
            ),
        },
        {
            "name": "Metadata-Extraktion verarbeitet",
            "values": [row["metadata_extraction_processed_total"] for row in rows],
            "color": "#7357d8",
            "legend_value_label": format_int(
                rows[-1]["metadata_extraction_processed_total"] if rows else 0
            ),
        },
    ]
    return {
        "note": None,
        "rows": rows,
        "chart": make_line_chart(labels, series, time_labels=time_labels),
    }


def _match_throughput_24h(engine, timings=None):
    rows = _timed_rows(
        engine,
        timings,
        "match score throughput database query",
        stats_sql.MATCH_THROUGHPUT_24H_SQL,
    )
    labels = [row["bucket"].strftime("%H:%M") for row in rows]
    time_labels = _chart_time_labels(rows, "time")
    return {
        "rows": rows,
        "chart": make_line_chart(
            labels,
            [
                {
                    "name": "Metadata Matching",
                    "values": [row["metadata_total"] for row in rows],
                    "color": "#e5532f",
                },
                {
                    "name": "Image Matching",
                    "values": [row["image_total"] for row in rows],
                    "color": "#18a558",
                },
            ],
            time_labels=time_labels,
        ),
    }


def _pipeline_throughput_24h(engine, timings=None):
    rows = _timed_rows(
        engine,
        timings,
        "auction pipeline throughput database query",
        stats_sql.PIPELINE_THROUGHPUT_24H_SQL,
    )
    labels = [row["bucket"].strftime("%H:%M") for row in rows]
    time_labels = _chart_time_labels(rows, "time")
    return {
        "rows": rows,
        "chart": make_line_chart(
            labels,
            [
                {
                    "name": "Bild-Matching",
                    "values": [row["image_matching_total"] for row in rows],
                    "color": "#18a558",
                },
                {
                    "name": "Metadata-Matching",
                    "values": [row["metadata_matching_total"] for row in rows],
                    "color": "#e5532f",
                },
                {
                    "name": "Metadata-Extraktion",
                    "values": [row["metadata_extraction_total"] for row in rows],
                    "color": "#7357d8",
                },
            ],
            time_labels=time_labels,
        ),
    }


def _image_file_metrics(engine, timings=None):
    rows = _timed_rows(
        engine,
        timings,
        "image file path database query",
        stats_sql.IMAGE_FILE_PATHS_SQL,
    )
    paths = [row["file_path"] for row in rows]
    if timings is None:
        return image_file_metrics(
            paths, refresh_async=True, include_scan_paths=True
        )
    with timings.measure("image filesystem size scan"):
        return image_file_metrics(
            paths, refresh_async=True, include_scan_paths=True
        )


def _with_fresh_image_file_metrics(stats):
    image_files = stats.get("image_files")
    if not isinstance(image_files, dict) or "_scan_paths" not in image_files:
        return stats
    return {
        **stats,
        "image_files": image_file_metrics(
            image_files["_scan_paths"],
            refresh_async=True,
            include_scan_paths=True,
        ),
    }


def _match_categories(engine, timings=None):
    row = _timed_rows(
        engine,
        timings,
        "match review status database query",
        stats_sql.MATCH_CATEGORIES_SQL,
    )[0]
    items = [
        {"label": "Unbewertet", "value": row["new_total"], "color": "#002851"},
        {"label": "Gemerkt", "value": row["bookmarked_total"], "color": "#7357d8"},
        {"label": "Akzeptiert", "value": row["accepted_total"], "color": "#18a558"},
        {"label": "Verworfen", "value": row["discarded_total"], "color": "#dc3545"},
    ]
    return make_bar_chart(items)


def _collect_dashboard_stats(connectable):
    timings = StatsTimingCollector()
    total_start = time.perf_counter()
    lost_count, auction_count = _dashboard_counts(connectable, timings)
    stats = {
        "lost_artwork_count": lost_count,
        "lost_artwork_label": format_int(lost_count),
        "auction_artwork_count": auction_count,
        "auction_artwork_label": format_int(auction_count),
        "auction_over_time": _auction_artworks_over_time(connectable, timings),
        "throughput": _match_throughput_24h(connectable, timings),
        "pipeline_throughput": _pipeline_throughput_24h(connectable, timings),
        "image_files": _image_file_metrics(connectable, timings),
        "match_categories": _match_categories(connectable, timings),
    }
    timings.add("total stats aggregation", time.perf_counter() - total_start)
    _log_dashboard_stats_timing(timings)
    return stats


def get_dashboard_stats(engine):
    """Collect all values and chart geometry for the /stats/ dashboard."""
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        with _connection(engine) as connection:
            return _collect_dashboard_stats(connection)

    key = _cache_key(engine)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached_stats = None
        if (
            _DASHBOARD_STATS_CACHE["key"] == key
            and _DASHBOARD_STATS_CACHE["value"] is not None
            and now < _DASHBOARD_STATS_CACHE["expires_at"]
        ):
            cached_stats = _DASHBOARD_STATS_CACHE["value"]

    if cached_stats is not None:
        return _with_fresh_image_file_metrics(cached_stats)

    with _CACHE_LOCK:
        now = time.monotonic()
        if (
            _DASHBOARD_STATS_CACHE["key"] == key
            and _DASHBOARD_STATS_CACHE["value"] is not None
            and now < _DASHBOARD_STATS_CACHE["expires_at"]
        ):
            cached_stats = _DASHBOARD_STATS_CACHE["value"]
        else:
            with _connection(engine) as connection:
                cached_stats = _collect_dashboard_stats(connection)
            _DASHBOARD_STATS_CACHE.update(
                {
                    "key": key,
                    "expires_at": time.monotonic() + ttl,
                    "value": cached_stats,
                }
            )

    return _with_fresh_image_file_metrics(cached_stats)
