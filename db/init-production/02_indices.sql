-- Aggregated production indices for the schema defined in 01_schema_production.sql.
-- Duplicate index definitions from historical migrations are intentionally kept once here.

CREATE INDEX IF NOT EXISTS idx_auction_artwork_image_file_image_file_id
    ON auction_artwork_image_file (image_file_id);

CREATE INDEX IF NOT EXISTS idx_auction_artwork_image_file_unprocessed
    ON auction_artwork_image_file (auction_artwork_id, image_file_id)
    WHERE is_image_matching_processed = false;

CREATE INDEX IF NOT EXISTS idx_lost_artwork_image_file_image_file_id
    ON lost_artwork_image_file (image_file_id);

CREATE INDEX IF NOT EXISTS idx_auction_artwork_metadata_unprocessed
    ON auction_artwork (auction_artwork_id)
    WHERE is_metadata_matching_processed = false;

CREATE INDEX IF NOT EXISTS idx_auction_artwork_unprocessed_extraction
    ON auction_artwork (auction_artwork_id)
    WHERE is_metadata_extraction_processed = false;

CREATE INDEX IF NOT EXISTS idx_auction_artwork_unprocessed_image_matching
    ON auction_artwork (auction_artwork_id)
    WHERE is_image_matching_processed = false;

CREATE INDEX IF NOT EXISTS idx_auction_artwork_image_matching_processed_at_desc
    ON auction_artwork (is_image_matching_processed_at DESC)
    WHERE is_image_matching_processed_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_auction_artwork_metadata_matching_processed_at_desc
    ON auction_artwork (is_metadata_matching_processed_at DESC)
    WHERE is_metadata_matching_processed_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_auction_artwork_metadata_extraction_processed_at_desc
    ON auction_artwork (is_metadata_extraction_processed_at DESC)
    WHERE is_metadata_extraction_processed_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_auction_artwork_created_at
    ON auction_artwork (created_at);

CREATE INDEX IF NOT EXISTS idx_scraper_run_name_started
    ON scraper_run (scraper_name, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_match_score_metadata_final_score_desc
    ON match_score (metadata_final_score DESC);

CREATE INDEX IF NOT EXISTS idx_match_score_metadata_confidence_score_desc
    ON match_score (metadata_confidence_score DESC);

CREATE INDEX IF NOT EXISTS idx_match_score_metadata_final_score_confidence_score_desc
    ON match_score (metadata_final_score DESC, metadata_confidence_score DESC);

CREATE INDEX IF NOT EXISTS idx_match_score_metadata_match_date_desc
    ON match_score (metadata_match_date DESC);

CREATE INDEX IF NOT EXISTS idx_match_score_image_matching_confidence_desc
    ON match_score (image_matching_confidence DESC);

CREATE INDEX IF NOT EXISTS idx_match_score_image_final_score_desc
    ON match_score (image_final_score DESC);

CREATE INDEX IF NOT EXISTS idx_match_score_image_blocking_similarity_desc
    ON match_score (image_blocking_similarity DESC);

CREATE INDEX IF NOT EXISTS idx_match_score_image_final_score_confidence_desc
    ON match_score (image_final_score DESC, image_matching_confidence DESC);

CREATE INDEX IF NOT EXISTS idx_match_score_image_match_date_desc
    ON match_score (image_match_date DESC);

CREATE INDEX IF NOT EXISTS idx_match_score_lost_id
    ON match_score (lost_id);

CREATE INDEX IF NOT EXISTS idx_match_score_auction_id
    ON match_score (auction_id);

CREATE INDEX IF NOT EXISTS idx_match_score_best_image_file_id
    ON match_score (best_image_file_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_auction_artwork_artist_id
    ON auction_artwork (artist_id);

CREATE INDEX IF NOT EXISTS idx_auction_artwork_platform_id
    ON auction_artwork (auction_platform_id);

CREATE INDEX IF NOT EXISTS idx_auction_artwork_auction_date
    ON auction_artwork (auction_date);

CREATE UNIQUE INDEX IF NOT EXISTS uq_auction_artwork_platform_lot_id
    ON auction_artwork (auction_platform_id, lot_id)
    WHERE lot_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_auction_artwork_platform_lot_url
    ON auction_artwork (auction_platform_id, lot_url)
    WHERE lot_url IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_artist_complete_name_ci
    ON artist ((lower(complete_name)));

CREATE UNIQUE INDEX IF NOT EXISTS uq_auction_platform_name_ci
    ON auction_platform ((lower(name)));

CREATE UNIQUE INDEX IF NOT EXISTS uq_auctioneer_name_ci
    ON auctioneer ((lower(name)));
