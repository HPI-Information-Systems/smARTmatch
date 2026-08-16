"""SQL statements for dashboard statistics."""

AUCTION_ARTWORKS_OVER_TIME_SQL = """
WITH event_counts AS (
    SELECT aa.created_at::date AS bucket,
           'total' AS kind,
           COUNT(*)::int AS daily_count
    FROM auction_artwork aa
    GROUP BY aa.created_at::date
    UNION ALL
    SELECT aa.is_image_matching_processed_at::date AS bucket,
           'image_matching' AS kind,
           COUNT(*)::int AS daily_count
    FROM auction_artwork aa
    WHERE aa.is_image_matching_processed_at IS NOT NULL
    GROUP BY aa.is_image_matching_processed_at::date
    UNION ALL
    SELECT aa.is_metadata_matching_processed_at::date AS bucket,
           'metadata_matching' AS kind,
           COUNT(*)::int AS daily_count
    FROM auction_artwork aa
    WHERE aa.is_metadata_matching_processed_at IS NOT NULL
    GROUP BY aa.is_metadata_matching_processed_at::date
    UNION ALL
    SELECT aa.is_metadata_extraction_processed_at::date AS bucket,
           'metadata_extraction' AS kind,
           COUNT(*)::int AS daily_count
    FROM auction_artwork aa
    WHERE aa.is_metadata_extraction_processed_at IS NOT NULL
    GROUP BY aa.is_metadata_extraction_processed_at::date
), daily_counts AS (
    SELECT event_counts.bucket,
           COALESCE(
               SUM(daily_count) FILTER (WHERE kind = 'total'), 0
           )::int AS daily_total,
           COALESCE(
               SUM(daily_count) FILTER (WHERE kind = 'image_matching'), 0
           )::int AS image_matching_processed_daily,
           COALESCE(
               SUM(daily_count) FILTER (WHERE kind = 'metadata_matching'), 0
           )::int AS metadata_matching_processed_daily,
           COALESCE(
               SUM(daily_count) FILTER (WHERE kind = 'metadata_extraction'), 0
           )::int AS metadata_extraction_processed_daily
    FROM event_counts
    WHERE event_counts.bucket IS NOT NULL
    GROUP BY event_counts.bucket
)
SELECT daily_counts.bucket,
       (
           SUM(daily_total) OVER (
               ORDER BY daily_counts.bucket
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
           )
       )::int AS total,
       (
           SUM(image_matching_processed_daily) OVER (
               ORDER BY daily_counts.bucket
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
           )
       )::int AS image_matching_processed_total,
       (
           SUM(metadata_matching_processed_daily) OVER (
               ORDER BY daily_counts.bucket
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
           )
       )::int AS metadata_matching_processed_total,
       (
           SUM(metadata_extraction_processed_daily) OVER (
               ORDER BY daily_counts.bucket
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
           )
       )::int AS metadata_extraction_processed_total
FROM daily_counts
ORDER BY daily_counts.bucket
"""

MATCH_THROUGHPUT_24H_SQL = """
WITH bounds AS (
    SELECT date_trunc('hour', now()) - interval '23 hours' AS start_hour,
           date_trunc('hour', now()) AS end_hour
), hours AS (
    SELECT generate_series(start_hour, end_hour, interval '1 hour') AS bucket
    FROM bounds
), metadata_counts AS (
    SELECT date_trunc('hour', ms.metadata_match_date) AS bucket,
           COUNT(*)::int AS total
    FROM match_score ms
    WHERE ms.metadata_final_score IS NOT NULL
      AND ms.metadata_match_date >= (SELECT start_hour FROM bounds)
      AND ms.metadata_match_date < (SELECT end_hour FROM bounds) + interval '1 hour'
    GROUP BY 1
), image_counts AS (
    SELECT date_trunc('hour', ms.image_match_date) AS bucket,
           COUNT(*)::int AS total
    FROM match_score ms
    WHERE (ms.image_final_score IS NOT NULL OR ms.image_matching_confidence IS NOT NULL)
      AND ms.image_match_date >= (SELECT start_hour FROM bounds)
      AND ms.image_match_date < (SELECT end_hour FROM bounds) + interval '1 hour'
    GROUP BY 1
)
SELECT hours.bucket,
       COALESCE(metadata_counts.total, 0)::int AS metadata_total,
       COALESCE(image_counts.total, 0)::int AS image_total
FROM hours
LEFT JOIN metadata_counts ON metadata_counts.bucket = hours.bucket
LEFT JOIN image_counts ON image_counts.bucket = hours.bucket
ORDER BY hours.bucket
"""

PIPELINE_THROUGHPUT_24H_SQL = """
WITH bounds AS (
    SELECT date_trunc('hour', now()) - interval '23 hours' AS start_hour,
           date_trunc('hour', now()) AS end_hour
), hours AS (
    SELECT generate_series(start_hour, end_hour, interval '1 hour') AS bucket
    FROM bounds
), processed_events AS (
    SELECT date_trunc('hour', processed.processed_at) AS bucket,
           processed.pipeline
    FROM auction_artwork aa
    CROSS JOIN bounds
    CROSS JOIN LATERAL (
        VALUES
            ('image_matching', aa.is_image_matching_processed_at),
            ('metadata_matching', aa.is_metadata_matching_processed_at),
            ('metadata_extraction', aa.is_metadata_extraction_processed_at)
    ) AS processed(pipeline, processed_at)
    WHERE processed.processed_at >= bounds.start_hour
      AND processed.processed_at < bounds.end_hour + interval '1 hour'
), pipeline_counts AS (
    SELECT bucket,
           COUNT(*) FILTER (WHERE pipeline = 'image_matching')::int
               AS image_matching_total,
           COUNT(*) FILTER (WHERE pipeline = 'metadata_matching')::int
               AS metadata_matching_total,
           COUNT(*) FILTER (WHERE pipeline = 'metadata_extraction')::int
               AS metadata_extraction_total
    FROM processed_events
    GROUP BY bucket
)
SELECT hours.bucket,
       COALESCE(pipeline_counts.image_matching_total, 0)::int AS image_matching_total,
       COALESCE(pipeline_counts.metadata_matching_total, 0)::int AS metadata_matching_total,
       COALESCE(pipeline_counts.metadata_extraction_total, 0)::int AS metadata_extraction_total
FROM hours
LEFT JOIN pipeline_counts ON pipeline_counts.bucket = hours.bucket
ORDER BY hours.bucket
"""

IMAGE_FILE_PATHS_SQL = """
SELECT file_path
FROM image_file
WHERE cleaned_up_at IS NULL
  AND file_path IS NOT NULL
"""

DASHBOARD_COUNTS_SQL = """
SELECT
    (SELECT COUNT(*)::int FROM lost_artwork) AS lost_artwork_count,
    (SELECT COUNT(*)::int FROM auction_artwork) AS auction_artwork_count
"""

MATCH_CATEGORIES_SQL = """
SELECT
    COUNT(*) FILTER (
        WHERE COALESCE(bookmarked, false) = false AND COALESCE(rating, 0) = 0
    )::int AS new_total,
    COUNT(*) FILTER (
        WHERE COALESCE(bookmarked, false) = true
    )::int AS bookmarked_total,
    COUNT(*) FILTER (WHERE COALESCE(rating, 0) > 0)::int AS accepted_total,
    COUNT(*) FILTER (WHERE COALESCE(rating, 0) < 0)::int AS discarded_total
FROM match_score
"""

LOST_ARTWORK_COUNT_SQL = "SELECT COUNT(*)::int FROM lost_artwork"
AUCTION_ARTWORK_COUNT_SQL = "SELECT COUNT(*)::int FROM auction_artwork"
