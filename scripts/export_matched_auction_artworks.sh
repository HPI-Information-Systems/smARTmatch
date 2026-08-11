#!/usr/bin/env bash
# Export matched auction artworks and their local images for transfer.
#
# Run from the repository root:
#   ./scripts/export_matched_auction_artworks.sh
#
# Outputs:
#   transfer/auction_artworks.sql
#   transfer/rsync-files.txt
#
# The SQL imports source auction rows and their supporting relations, but no
# match_score rows. Imported artworks and images are reset for image/metadata
# matching; completed metadata extraction is retained.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./scripts/export_matched_auction_artworks.sh

Environment:
  ENV_FILE             Direct-mode env file (default: ./.env.docker)
  DB_MODE              compose or direct (default: compose)
  DB_SERVICE           Docker Compose database service (default: db)
  PSQL_BIN             psql executable for direct mode (default: psql)
  TRANSFER_DIR         Output directory (default: ./transfer)
  DB_IMAGE_ROOT        Image root used inside the source deployment
                       (default: SMARTMATCH_IMAGES_DIR or /app/db/images)
  TRANSFER_IMAGE_PREFIX Canonical repo-relative image prefix stored in the dump
                       (default: db/images)

Direct mode also uses POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB,
POSTGRES_USER, and POSTGRES_PASSWORD.

Outputs:
  transfer/auction_artworks.sql
  transfer/rsync-files.txt

Copy the selected images from the repository root with, for example:
  rsync -a --files-from=transfer/rsync-files.txt ./ user@host:/path/to/smARTmatch/
EOF
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
    usage
    exit 0
fi
[[ $# -eq 0 ]] || fail "unexpected arguments (use --help)"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ "$(pwd -P)" == "$ROOT_DIR" ]] || fail "run this script from the repository root: $ROOT_DIR"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.docker}"
DB_MODE="${DB_MODE:-compose}"
DB_SERVICE="${DB_SERVICE:-db}"
PSQL_BIN="${PSQL_BIN:-psql}"
TRANSFER_DIR="${TRANSFER_DIR:-$ROOT_DIR/transfer}"
DB_IMAGE_ROOT="${DB_IMAGE_ROOT:-${SMARTMATCH_IMAGES_DIR:-/app/db/images}}"
TRANSFER_IMAGE_PREFIX="${TRANSFER_IMAGE_PREFIX:-db/images}"
DB_IMAGE_ROOT="${DB_IMAGE_ROOT%/}"
TRANSFER_IMAGE_PREFIX="${TRANSFER_IMAGE_PREFIX#./}"
TRANSFER_IMAGE_PREFIX="${TRANSFER_IMAGE_PREFIX%/}"

command -v python3 >/dev/null 2>&1 || fail "python3 is required"
[[ -n "$DB_IMAGE_ROOT" ]] || fail "DB_IMAGE_ROOT must not be empty"
[[ -n "$TRANSFER_IMAGE_PREFIX" ]] || fail "TRANSFER_IMAGE_PREFIX must not be empty"
[[ "$TRANSFER_IMAGE_PREFIX" != /* ]] || fail "TRANSFER_IMAGE_PREFIX must be repo-relative"
[[ "$TRANSFER_IMAGE_PREFIX" != \#* ]] || fail "TRANSFER_IMAGE_PREFIX must not begin with #"
[[ "$TRANSFER_IMAGE_PREFIX" != *$'\n'* && "$TRANSFER_IMAGE_PREFIX" != *$'\r'* ]] || \
    fail "TRANSFER_IMAGE_PREFIX must not contain newlines"

case "$DB_MODE" in
    compose)
        command -v docker >/dev/null 2>&1 || fail "docker is required for DB_MODE=compose"
        database_label="Docker Compose service $DB_SERVICE"
        ;;
    direct)
        if [[ -f "$ENV_FILE" ]]; then
            set -a
            # Direct-mode env files in this repository use shell-compatible KEY=VALUE syntax.
            # shellcheck disable=SC1090
            source "$ENV_FILE"
            set +a
        fi
        command -v "$PSQL_BIN" >/dev/null 2>&1 || fail "$PSQL_BIN is required for DB_MODE=direct"
        : "${POSTGRES_HOST:?POSTGRES_HOST must be set for DB_MODE=direct}"
        : "${POSTGRES_PORT:?POSTGRES_PORT must be set for DB_MODE=direct}"
        : "${POSTGRES_DB:?POSTGRES_DB must be set for DB_MODE=direct}"
        : "${POSTGRES_USER:?POSTGRES_USER must be set for DB_MODE=direct}"
        : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set for DB_MODE=direct}"
        database_label="$POSTGRES_DB@$POSTGRES_HOST:$POSTGRES_PORT"
        ;;
    *) fail "DB_MODE must be 'compose' or 'direct'" ;;
esac

run_psql() {
    if [[ "$DB_MODE" == "compose" ]]; then
        docker compose exec -T "$DB_SERVICE" sh -c '
            : "${POSTGRES_USER:?POSTGRES_USER is missing in the database container}"
            : "${POSTGRES_DB:?POSTGRES_DB is missing in the database container}"
            : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is missing in the database container}"
            export PGPASSWORD="$POSTGRES_PASSWORD"
            exec psql -X -qAt -v ON_ERROR_STOP=1 \
                -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"
        ' sh "$@"
    else
        PGPASSWORD="$POSTGRES_PASSWORD" "$PSQL_BIN" \
            -X -qAt -v ON_ERROR_STOP=1 \
            -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" \
            -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"
    fi
}

mkdir -p "$TRANSFER_DIR"
work_dir="$(mktemp -d "$TRANSFER_DIR/.matched-auction-export.XXXXXX")"
cleanup() {
    rm -rf "$work_dir"
}
trap cleanup EXIT

bundle="$work_dir/export.bundle"
sql_output="$work_dir/auction_artworks.sql"
path_hex_output="$work_dir/image-paths.hex"
rsync_output="$work_dir/rsync-files.txt"
path_marker="__SMARTMATCH_IMAGE_PATHS_HEX_BEGIN__"
path_end_marker="__SMARTMATCH_IMAGE_PATHS_HEX_END__"

echo "Exporting matched auction artworks from $database_label ..." >&2
if ! run_psql \
    -R '' \
    -v "db_image_root=$DB_IMAGE_ROOT" \
    -v "image_prefix=$TRANSFER_IMAGE_PREFIX" >"$bundle" <<'SOURCE_SQL'
-- CREATE TEMP TABLE ... AS requires a read-write transaction in PostgreSQL.
-- This transaction never writes permanent tables and is rolled back below.
BEGIN ISOLATION LEVEL REPEATABLE READ, READ WRITE;
SET LOCAL search_path = pg_temp, public, pg_catalog;

DO $$
DECLARE
    relation_name text;
    missing_columns text;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY[
        'match_score', 'auction_artwork', 'artist', 'artist_name_variants',
        'location', 'location_name_variants', 'country', 'auction_platform',
        'auctioneer', 'expert', 'dict_material', 'material_variant',
        'dict_technique', 'technique_variant', 'image_file',
        'auction_artwork_image_file'
    ] LOOP
        IF to_regclass(format('public.%I', relation_name)) IS NULL THEN
            RAISE EXCEPTION 'Required table public.% is missing', relation_name;
        END IF;
    END LOOP;

    SELECT string_agg(required.table_name || '.' || required.column_name, ', ')
    INTO missing_columns
    FROM (VALUES
        ('match_score', 'auction_id'),
        ('auction_artwork', 'auction_artwork_id'),
        ('auction_artwork', 'created_at'),
        ('auction_artwork', 'is_metadata_matching_processed'),
        ('auction_artwork', 'is_metadata_extraction_processed'),
        ('auction_artwork', 'is_image_matching_processed'),
        ('image_file', 'file_path'),
        ('image_file', 'is_embedded'),
        ('auction_artwork_image_file', 'is_image_matching_processed')
    ) AS required(table_name, column_name)
    LEFT JOIN information_schema.columns actual
      ON actual.table_schema = 'public'
     AND actual.table_name = required.table_name
     AND actual.column_name = required.column_name
    WHERE actual.column_name IS NULL;

    IF missing_columns IS NOT NULL THEN
        RAISE EXCEPTION 'Source schema is not current; missing columns: %', missing_columns;
    END IF;
END $$;

CREATE TEMP TABLE _selected_auction_id ON COMMIT DROP AS
SELECT DISTINCT ms.auction_id AS auction_artwork_id
FROM match_score ms
JOIN auction_artwork aa ON aa.auction_artwork_id = ms.auction_id;
CREATE UNIQUE INDEX ON _selected_auction_id (auction_artwork_id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM _selected_auction_id) THEN
        RAISE EXCEPTION 'No auction artworks with match_score rows were found';
    END IF;
END $$;

CREATE TEMP TABLE _selected_artist_id ON COMMIT DROP AS
SELECT DISTINCT aa.artist_id
FROM auction_artwork aa
JOIN _selected_auction_id selected USING (auction_artwork_id)
WHERE aa.artist_id IS NOT NULL;

CREATE TEMP TABLE _selected_location_id ON COMMIT DROP AS
SELECT location_id
FROM (
    SELECT aa.artist_birth_place AS location_id
    FROM auction_artwork aa
    JOIN _selected_auction_id selected USING (auction_artwork_id)
    UNION
    SELECT aa.artist_death_place
    FROM auction_artwork aa
    JOIN _selected_auction_id selected USING (auction_artwork_id)
    UNION
    SELECT artist.place_of_birth
    FROM artist
    JOIN _selected_artist_id selected USING (artist_id)
    UNION
    SELECT artist.place_of_death
    FROM artist
    JOIN _selected_artist_id selected USING (artist_id)
    UNION
    SELECT unnest(COALESCE(artist.activity_place_ids, '{}'::uuid[]))
    FROM artist
    JOIN _selected_artist_id selected USING (artist_id)
) locations
WHERE location_id IS NOT NULL;
CREATE UNIQUE INDEX ON _selected_location_id (location_id);

CREATE TEMP TABLE _selected_country_id ON COMMIT DROP AS
SELECT country_id
FROM (
    SELECT unnest(COALESCE(location.country_ids, '{}'::uuid[])) AS country_id
    FROM location
    JOIN _selected_location_id selected USING (location_id)
    UNION
    SELECT unnest(COALESCE(artist.associated_country_ids, '{}'::uuid[]))
    FROM artist
    JOIN _selected_artist_id selected USING (artist_id)
) countries
WHERE country_id IS NOT NULL;
CREATE UNIQUE INDEX ON _selected_country_id (country_id);

CREATE TEMP TABLE _selected_material ON COMMIT DROP AS
WITH RECURSIVE names(material_name) AS (
    SELECT unnest(COALESCE(aa.dict_material_name, '{}'::varchar[]))
    FROM auction_artwork aa
    JOIN _selected_auction_id selected USING (auction_artwork_id)
    UNION
    SELECT dict.material_parent
    FROM dict_material dict
    JOIN names ON names.material_name = dict.material_name
    WHERE dict.material_parent IS NOT NULL
)
SELECT dict.*
FROM dict_material dict
JOIN names USING (material_name);

CREATE TEMP TABLE _selected_technique ON COMMIT DROP AS
WITH RECURSIVE names(technique_name) AS (
    SELECT unnest(COALESCE(aa.dict_technique_name, '{}'::varchar[]))
    FROM auction_artwork aa
    JOIN _selected_auction_id selected USING (auction_artwork_id)
    UNION
    SELECT dict.technique_parent
    FROM dict_technique dict
    JOIN names ON names.technique_name = dict.technique_name
    WHERE dict.technique_parent IS NOT NULL
)
SELECT dict.*
FROM dict_technique dict
JOIN names USING (technique_name);

CREATE TEMP TABLE _transfer_settings ON COMMIT DROP AS
SELECT rtrim(:'db_image_root', '/')::text AS db_image_root,
       rtrim(regexp_replace(:'image_prefix', '^\./', ''), '/')::text AS image_prefix;

CREATE TEMP TABLE _selected_image_file ON COMMIT DROP AS
SELECT img.image_file_id,
       CASE
           WHEN cleaned.file_path = settings.image_prefix
               OR left(cleaned.file_path, length(settings.image_prefix) + 1)
                   = settings.image_prefix || '/'
               THEN cleaned.file_path
           WHEN settings.db_image_root <> ''
               AND left(cleaned.file_path, length(settings.db_image_root) + 1)
                   = settings.db_image_root || '/'
               THEN settings.image_prefix || substr(cleaned.file_path, length(settings.db_image_root) + 1)
           WHEN left(cleaned.file_path, 1) <> '/'
               THEN settings.image_prefix || '/' || cleaned.file_path
           ELSE cleaned.file_path
       END AS file_path
FROM image_file img
JOIN auction_artwork_image_file link USING (image_file_id)
JOIN _selected_auction_id selected USING (auction_artwork_id)
CROSS JOIN _transfer_settings settings
CROSS JOIN LATERAL (
    SELECT regexp_replace(btrim(img.file_path), '^\./', '') AS file_path
) cleaned
GROUP BY img.image_file_id, cleaned.file_path, settings.db_image_root, settings.image_prefix;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM _selected_image_file
        WHERE file_path = '' OR file_path ~ E'[\n\r]'
    ) THEN
        RAISE EXCEPTION 'Selected image_file paths must be non-empty and may not contain newlines';
    END IF;
    IF EXISTS (
        SELECT 1 FROM _selected_image_file
        WHERE string_to_array(file_path, '/') @> ARRAY['..']::text[]
    ) THEN
        RAISE EXCEPTION 'Selected image_file paths may not contain .. path components';
    END IF;
    IF EXISTS (
        SELECT 1 FROM _selected_image_file
        WHERE left(file_path, 1) = '/'
    ) THEN
        RAISE EXCEPTION 'Selected image paths remain absolute; set DB_IMAGE_ROOT to their source image root';
    END IF;
END $$;

SELECT $export$
-- =============================================================================
-- Matched auction-artwork transfer
-- Generated by scripts/export_matched_auction_artworks.sh
--
-- Contains only auction artworks that had source match_score rows and their
-- supporting relations. It intentionally contains no match_score rows.
-- Auction artworks already present on the target are skipped by UUID or
-- platform-scoped lot identity; target rows and match data are never changed.
--
-- Restore:
--   psql -v ON_ERROR_STOP=1 -h <host> -U <user> -d <database> \
--     < transfer/auction_artworks.sql
-- =============================================================================
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path = pg_temp, public, pg_catalog;

CREATE TEMP TABLE _transfer_country (LIKE country INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_country (country_id, country_name, country_name_en) FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT country.country_id, country.country_name, country.country_name_en
    FROM country
    JOIN _selected_country_id selected USING (country_id)
    ORDER BY country.country_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_location (LIKE location INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_location (
    location_id, location_name, location_name_en, country, country_en, raw_data,
    lat, lon, historical_info, display_name, country_ids
) FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT location.location_id, location.location_name, location.location_name_en,
           location.country, location.country_en, location.raw_data, location.lat,
           location.lon, location.historical_info, location.display_name,
           location.country_ids
    FROM location
    JOIN _selected_location_id selected USING (location_id)
    ORDER BY location.location_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_location_name_variants
    (LIKE location_name_variants INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_location_name_variants (name_variant_id, location_id, name_variant)
    FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT variant.name_variant_id, variant.location_id, variant.name_variant
    FROM location_name_variants variant
    JOIN _selected_location_id selected USING (location_id)
    ORDER BY variant.name_variant_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_artist (LIKE artist INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_artist (
    artist_id, complete_name, date_of_birth, date_of_death, place_of_birth,
    place_of_death, raw_data, gender, period_of_activity, preferred_name,
    surname, title_of_nobility, profession, biographical_info,
    associated_country_ids, activity_place_ids
) FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT artist.artist_id, artist.complete_name, artist.date_of_birth,
           artist.date_of_death, artist.place_of_birth, artist.place_of_death,
           artist.raw_data, artist.gender, artist.period_of_activity,
           artist.preferred_name, artist.surname, artist.title_of_nobility,
           artist.profession, artist.biographical_info,
           artist.associated_country_ids, artist.activity_place_ids
    FROM artist
    JOIN _selected_artist_id selected USING (artist_id)
    ORDER BY artist.artist_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_artist_name_variants
    (LIKE artist_name_variants INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_artist_name_variants (name_variant_id, artist_id, name_variant)
    FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT variant.name_variant_id, variant.artist_id, variant.name_variant
    FROM artist_name_variants variant
    JOIN _selected_artist_id selected USING (artist_id)
    ORDER BY variant.name_variant_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_auction_platform
    (LIKE auction_platform INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_auction_platform
    (auction_platform_id, name, address, phone, email, raw_data)
    FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT DISTINCT platform.auction_platform_id, platform.name, platform.address,
           platform.phone, platform.email, platform.raw_data
    FROM auction_platform platform
    JOIN auction_artwork aa USING (auction_platform_id)
    JOIN _selected_auction_id selected USING (auction_artwork_id)
    ORDER BY platform.auction_platform_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_auctioneer
    (LIKE auctioneer INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_auctioneer (auctioneer_id, name, address, phone, email, raw_data)
    FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT DISTINCT auctioneer.auctioneer_id, auctioneer.name, auctioneer.address,
           auctioneer.phone, auctioneer.email, auctioneer.raw_data
    FROM auctioneer
    JOIN auction_artwork aa USING (auctioneer_id)
    JOIN _selected_auction_id selected USING (auction_artwork_id)
    ORDER BY auctioneer.auctioneer_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_expert (LIKE expert INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_expert
    (expert_id, first_name, last_name, organization, phone, email, raw_data)
    FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT DISTINCT expert.expert_id, expert.first_name, expert.last_name,
           expert.organization, expert.phone, expert.email, expert.raw_data
    FROM expert
    JOIN auction_artwork aa USING (expert_id)
    JOIN _selected_auction_id selected USING (auction_artwork_id)
    ORDER BY expert.expert_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_dict_material
    (LIKE dict_material INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_dict_material (material_name, material_parent) FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT material_name, material_parent
    FROM _selected_material
    ORDER BY material_name
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_material_variant
    (LIKE material_variant INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_material_variant
    (material_variant_id, dict_material_name, material_raw_data, match_type)
    FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT variant.material_variant_id, variant.dict_material_name,
           variant.material_raw_data, variant.match_type
    FROM material_variant variant
    JOIN _selected_material selected
      ON selected.material_name = variant.dict_material_name
    ORDER BY variant.material_variant_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_dict_technique
    (LIKE dict_technique INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_dict_technique (technique_name, technique_parent) FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT technique_name, technique_parent
    FROM _selected_technique
    ORDER BY technique_name
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_technique_variant
    (LIKE technique_variant INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_technique_variant
    (technique_variant_id, dict_technique_name, technique_raw_data, match_type)
    FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT variant.technique_variant_id, variant.dict_technique_name,
           variant.technique_raw_data, variant.match_type
    FROM technique_variant variant
    JOIN _selected_technique selected
      ON selected.technique_name = variant.dict_technique_name
    ORDER BY variant.technique_variant_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_auction_artwork
    (LIKE auction_artwork INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_auction_artwork (
    auction_artwork_id, created_at, normalised_version, title, artist_id,
    artist_full_name, artist_birth_date, artist_death_date, artist_birth_place,
    artist_death_place, width, height, width_frame, height_frame, dating,
    dating_start, dating_end, description, auction_details, material,
    dict_material_name, technique, dict_technique_name, provenance, auction_date,
    lot_id, lot_url, auction_platform_id, auctioneer_id, expert_id, condition,
    signature, literature, raw_data, artist_raw_data, date_of_birth_raw_data,
    date_of_death_raw_data, place_of_birth_raw_data, place_of_death_raw_data,
    dimensions_raw_data, is_metadata_matching_processed,
    is_metadata_matching_processed_at, is_metadata_extraction_processed,
    is_metadata_extraction_processed_at, is_image_matching_processed,
    is_image_matching_processed_at
) FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT aa.auction_artwork_id, aa.created_at, aa.normalised_version, aa.title,
           aa.artist_id, aa.artist_full_name, aa.artist_birth_date,
           aa.artist_death_date, aa.artist_birth_place, aa.artist_death_place,
           aa.width, aa.height, aa.width_frame, aa.height_frame, aa.dating,
           aa.dating_start, aa.dating_end, aa.description, aa.auction_details,
           aa.material, aa.dict_material_name, aa.technique,
           aa.dict_technique_name, aa.provenance, aa.auction_date, aa.lot_id,
           aa.lot_url, aa.auction_platform_id, aa.auctioneer_id, aa.expert_id,
           aa.condition, aa.signature, aa.literature, aa.raw_data,
           aa.artist_raw_data, aa.date_of_birth_raw_data,
           aa.date_of_death_raw_data, aa.place_of_birth_raw_data,
           aa.place_of_death_raw_data, aa.dimensions_raw_data,
           false, NULL::timestamptz,
           aa.is_metadata_extraction_processed,
           aa.is_metadata_extraction_processed_at,
           CASE WHEN EXISTS (
               SELECT 1 FROM auction_artwork_image_file link
               WHERE link.auction_artwork_id = aa.auction_artwork_id
           ) THEN false ELSE true END,
           CASE WHEN EXISTS (
               SELECT 1 FROM auction_artwork_image_file link
               WHERE link.auction_artwork_id = aa.auction_artwork_id
           ) THEN NULL::timestamptz ELSE aa.is_image_matching_processed_at END
    FROM auction_artwork aa
    JOIN _selected_auction_id selected USING (auction_artwork_id)
    ORDER BY aa.auction_artwork_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_image_file
    (LIKE image_file INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_image_file (image_file_id, file_path, is_embedded)
    FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT image_file_id, file_path, false
    FROM _selected_image_file
    ORDER BY image_file_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
CREATE TEMP TABLE _transfer_auction_artwork_image_file
    (LIKE auction_artwork_image_file INCLUDING DEFAULTS) ON COMMIT DROP;
COPY _transfer_auction_artwork_image_file
    (auction_artwork_id, image_file_id, is_image_matching_processed)
    FROM STDIN (FORMAT text);
$export$;
COPY (
    SELECT link.auction_artwork_id, link.image_file_id, false
    FROM auction_artwork_image_file link
    JOIN _selected_auction_id selected USING (auction_artwork_id)
    ORDER BY link.auction_artwork_id, link.image_file_id
) TO STDOUT (FORMAT text);
SELECT $copy_end$\.$copy_end$;

SELECT $export$
-- Insert UUID-backed support rows before auction artworks.
INSERT INTO country SELECT * FROM _transfer_country
ON CONFLICT (country_id) DO NOTHING;

INSERT INTO location SELECT * FROM _transfer_location
ON CONFLICT (location_id) DO NOTHING;

INSERT INTO location_name_variants SELECT * FROM _transfer_location_name_variants
ON CONFLICT (name_variant_id) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM _transfer_artist source
        JOIN artist target USING (artist_id)
        WHERE lower(source.complete_name) <> lower(target.complete_name)
    ) THEN
        RAISE EXCEPTION 'artist UUID collision with a different complete_name';
    END IF;
END $$;

INSERT INTO artist
SELECT source.*
FROM _transfer_artist source
WHERE NOT EXISTS (
    SELECT 1 FROM artist target
    WHERE target.artist_id = source.artist_id
       OR lower(target.complete_name) = lower(source.complete_name)
)
ON CONFLICT (artist_id) DO NOTHING;

CREATE TEMP TABLE _transfer_artist_id_map ON COMMIT DROP AS
SELECT source.artist_id AS source_id, target.artist_id AS target_id
FROM _transfer_artist source
JOIN LATERAL (
    SELECT candidate.artist_id
    FROM artist candidate
    WHERE candidate.artist_id = source.artist_id
       OR lower(candidate.complete_name) = lower(source.complete_name)
    ORDER BY (candidate.artist_id = source.artist_id) DESC, candidate.artist_id
    LIMIT 1
) target ON true;

INSERT INTO artist_name_variants (name_variant_id, artist_id, name_variant)
SELECT variant.name_variant_id, map.target_id, variant.name_variant
FROM _transfer_artist_name_variants variant
JOIN _transfer_artist_id_map map ON map.source_id = variant.artist_id
ON CONFLICT (name_variant_id) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM _transfer_auction_platform source
        JOIN auction_platform target USING (auction_platform_id)
        WHERE lower(source.name) <> lower(target.name)
    ) THEN
        RAISE EXCEPTION 'auction_platform UUID collision with a different name';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM _transfer_auctioneer source
        JOIN auctioneer target USING (auctioneer_id)
        WHERE lower(source.name) <> lower(target.name)
    ) THEN
        RAISE EXCEPTION 'auctioneer UUID collision with a different name';
    END IF;
END $$;

INSERT INTO auction_platform
SELECT source.*
FROM _transfer_auction_platform source
WHERE NOT EXISTS (
    SELECT 1 FROM auction_platform target
    WHERE target.auction_platform_id = source.auction_platform_id
       OR lower(target.name) = lower(source.name)
)
ON CONFLICT (auction_platform_id) DO NOTHING;

CREATE TEMP TABLE _transfer_platform_id_map ON COMMIT DROP AS
SELECT source.auction_platform_id AS source_id,
       target.auction_platform_id AS target_id
FROM _transfer_auction_platform source
JOIN LATERAL (
    SELECT candidate.auction_platform_id
    FROM auction_platform candidate
    WHERE candidate.auction_platform_id = source.auction_platform_id
       OR lower(candidate.name) = lower(source.name)
    ORDER BY (candidate.auction_platform_id = source.auction_platform_id) DESC,
             candidate.auction_platform_id
    LIMIT 1
) target ON true;

INSERT INTO auctioneer
SELECT source.*
FROM _transfer_auctioneer source
WHERE NOT EXISTS (
    SELECT 1 FROM auctioneer target
    WHERE target.auctioneer_id = source.auctioneer_id
       OR lower(target.name) = lower(source.name)
)
ON CONFLICT (auctioneer_id) DO NOTHING;

CREATE TEMP TABLE _transfer_auctioneer_id_map ON COMMIT DROP AS
SELECT source.auctioneer_id AS source_id, target.auctioneer_id AS target_id
FROM _transfer_auctioneer source
JOIN LATERAL (
    SELECT candidate.auctioneer_id
    FROM auctioneer candidate
    WHERE candidate.auctioneer_id = source.auctioneer_id
       OR lower(candidate.name) = lower(source.name)
    ORDER BY (candidate.auctioneer_id = source.auctioneer_id) DESC,
             candidate.auctioneer_id
    LIMIT 1
) target ON true;

INSERT INTO expert SELECT * FROM _transfer_expert
ON CONFLICT (expert_id) DO NOTHING;

INSERT INTO dict_material SELECT * FROM _transfer_dict_material
ON CONFLICT (material_name) DO NOTHING;

INSERT INTO material_variant SELECT * FROM _transfer_material_variant
-- Variants may already exist under a different UUID but the same natural key.
ON CONFLICT DO NOTHING;

INSERT INTO dict_technique SELECT * FROM _transfer_dict_technique
ON CONFLICT (technique_name) DO NOTHING;

INSERT INTO technique_variant SELECT * FROM _transfer_technique_variant
-- Variants may already exist under a different UUID but the same natural key.
ON CONFLICT DO NOTHING;

-- Existing UUIDs and platform-scoped lot identities may carry target-generated
-- matches or review state. Exclude them instead of overwriting or linking images.
CREATE TEMP TABLE _transfer_auction_id_map (
    source_id uuid PRIMARY KEY,
    target_id uuid NOT NULL UNIQUE
) ON COMMIT DROP;
INSERT INTO _transfer_auction_id_map (source_id, target_id)
SELECT source.auction_artwork_id, source.auction_artwork_id
FROM _transfer_auction_artwork source
LEFT JOIN _transfer_platform_id_map platform_map
  ON platform_map.source_id = source.auction_platform_id
WHERE NOT EXISTS (
    SELECT 1
    FROM auction_artwork target
    WHERE target.auction_artwork_id = source.auction_artwork_id
       OR (
           platform_map.target_id IS NOT NULL
           AND target.auction_platform_id = platform_map.target_id
           AND (
               (source.lot_id IS NOT NULL AND target.lot_id = source.lot_id)
               OR (source.lot_url IS NOT NULL AND target.lot_url = source.lot_url)
           )
       )
);

INSERT INTO auction_artwork (
    auction_artwork_id, created_at, normalised_version, title, artist_id,
    artist_full_name, artist_birth_date, artist_death_date, artist_birth_place,
    artist_death_place, width, height, width_frame, height_frame, dating,
    dating_start, dating_end, description, auction_details, material,
    dict_material_name, technique, dict_technique_name, provenance, auction_date,
    lot_id, lot_url, auction_platform_id, auctioneer_id, expert_id, condition,
    signature, literature, raw_data, artist_raw_data, date_of_birth_raw_data,
    date_of_death_raw_data, place_of_birth_raw_data, place_of_death_raw_data,
    dimensions_raw_data, is_metadata_matching_processed,
    is_metadata_matching_processed_at, is_metadata_extraction_processed,
    is_metadata_extraction_processed_at, is_image_matching_processed,
    is_image_matching_processed_at
)
SELECT source.auction_artwork_id, source.created_at, source.normalised_version,
       source.title, artist_map.target_id, source.artist_full_name,
       source.artist_birth_date, source.artist_death_date,
       source.artist_birth_place, source.artist_death_place, source.width,
       source.height, source.width_frame, source.height_frame, source.dating,
       source.dating_start, source.dating_end, source.description,
       source.auction_details, source.material, source.dict_material_name,
       source.technique, source.dict_technique_name, source.provenance,
       source.auction_date, source.lot_id, source.lot_url,
       platform_map.target_id, auctioneer_map.target_id, source.expert_id,
       source.condition, source.signature, source.literature, source.raw_data,
       source.artist_raw_data, source.date_of_birth_raw_data,
       source.date_of_death_raw_data, source.place_of_birth_raw_data,
       source.place_of_death_raw_data, source.dimensions_raw_data,
       source.is_metadata_matching_processed,
       source.is_metadata_matching_processed_at,
       source.is_metadata_extraction_processed,
       source.is_metadata_extraction_processed_at,
       source.is_image_matching_processed,
       source.is_image_matching_processed_at
FROM _transfer_auction_artwork source
JOIN _transfer_auction_id_map auction_map
  ON auction_map.source_id = source.auction_artwork_id
LEFT JOIN _transfer_artist_id_map artist_map
  ON artist_map.source_id = source.artist_id
LEFT JOIN _transfer_platform_id_map platform_map
  ON platform_map.source_id = source.auction_platform_id
LEFT JOIN _transfer_auctioneer_id_map auctioneer_map
  ON auctioneer_map.source_id = source.auctioneer_id;

-- Image IDs are deployment-local serials. Reuse/allocate by canonical file_path
-- and map source IDs instead of assuming they are portable.
INSERT INTO image_file (file_path, is_embedded)
SELECT DISTINCT source.file_path, false
FROM _transfer_image_file source
JOIN _transfer_auction_artwork_image_file link
  ON link.image_file_id = source.image_file_id
JOIN _transfer_auction_id_map auction_map
  ON auction_map.source_id = link.auction_artwork_id
WHERE NOT EXISTS (
    SELECT 1 FROM image_file target WHERE target.file_path = source.file_path
);

CREATE TEMP TABLE _transfer_image_id_map ON COMMIT DROP AS
SELECT source.image_file_id AS source_id,
       min(target.image_file_id) AS target_id
FROM _transfer_image_file source
JOIN _transfer_auction_artwork_image_file link
  ON link.image_file_id = source.image_file_id
JOIN _transfer_auction_id_map auction_map
  ON auction_map.source_id = link.auction_artwork_id
JOIN image_file target ON target.file_path = source.file_path
GROUP BY source.image_file_id;

UPDATE image_file target
SET is_embedded = false
FROM _transfer_image_id_map map
WHERE target.image_file_id = map.target_id;

INSERT INTO auction_artwork_image_file (
    auction_artwork_id, image_file_id, is_image_matching_processed
)
SELECT DISTINCT auction_map.target_id, image_map.target_id, false
FROM _transfer_auction_artwork_image_file source
JOIN _transfer_auction_id_map auction_map
  ON auction_map.source_id = source.auction_artwork_id
JOIN _transfer_image_id_map image_map
  ON image_map.source_id = source.image_file_id
ON CONFLICT (auction_artwork_id, image_file_id) DO UPDATE
SET is_image_matching_processed = false;

COMMIT;
$export$;

SELECT E'__SMARTMATCH_IMAGE_PATHS_HEX_BEGIN__\n';
COPY (
    SELECT encode(convert_to(file_path, 'UTF8'), 'hex')
    FROM _selected_image_file
    GROUP BY file_path
    ORDER BY file_path
) TO STDOUT;
SELECT E'__SMARTMATCH_IMAGE_PATHS_HEX_END__\n';

ROLLBACK;
SOURCE_SQL
then
    fail "database export failed; no artifacts were replaced"
fi

[[ $(grep -Fxc "$path_marker" "$bundle") -eq 1 ]] || fail "invalid export stream: missing path marker"
[[ $(grep -Fxc "$path_end_marker" "$bundle") -eq 1 ]] || fail "invalid export stream: missing path end marker"

awk -v begin="$path_marker" -v end="$path_end_marker" \
    -v sql="$sql_output" -v paths="$path_hex_output" '
    $0 == begin { in_paths = 1; next }
    $0 == end { in_paths = 0; ended = 1; next }
    in_paths { print > paths; next }
    !ended { print > sql }
' "$bundle"

[[ -s "$sql_output" ]] || fail "generated SQL artifact is empty"
grep -Fq 'COMMIT;' "$sql_output" || fail "generated SQL artifact is incomplete"
# awk creates no path file when the selected artworks have no images.
[[ -f "$path_hex_output" ]] || : >"$path_hex_output"

python3 - "$path_hex_output" "$rsync_output" "$ROOT_DIR" "$TRANSFER_IMAGE_PREFIX" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

hex_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
repo_root = Path(sys.argv[3]).resolve()
image_prefix = sys.argv[4].strip("/")
resolved_paths: set[str] = set()
missing: list[str] = []

for line_number, raw_hex in enumerate(hex_path.read_text(encoding="ascii").splitlines(), 1):
    try:
        raw_path = bytes.fromhex(raw_hex).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise SystemExit(f"Invalid encoded image path at line {line_number}: {exc}")
    if not raw_path or "\n" in raw_path or "\r" in raw_path:
        raise SystemExit(f"Invalid image path at line {line_number}: {raw_path!r}")

    path = Path(raw_path)
    if path.is_absolute():
        raise SystemExit(
            "Selected DB image path was not made repo-relative: "
            f"{raw_path!r}. Set DB_IMAGE_ROOT to the source deployment image root."
        )
    relative = Path(os.path.normpath(raw_path))

    if relative.is_absolute() or relative == Path("..") or ".." in relative.parts:
        raise SystemExit(f"Image path escapes the repository: {raw_path!r}")
    if relative.parts[: len(Path(image_prefix).parts)] != Path(image_prefix).parts:
        raise SystemExit(
            f"Image path is outside {image_prefix!r}: {raw_path!r}. "
            "Check DB_IMAGE_ROOT and TRANSFER_IMAGE_PREFIX."
        )

    local_path = repo_root / relative
    if not local_path.is_file() or not os.access(local_path, os.R_OK):
        missing.append(relative.as_posix())
    else:
        try:
            local_path.resolve(strict=True).relative_to(repo_root)
        except ValueError:
            raise SystemExit(f"Image path resolves outside the repository: {raw_path!r}")
    resolved_paths.add(relative.as_posix())

if missing:
    preview = "\n  ".join(sorted(missing)[:20])
    suffix = "" if len(missing) <= 20 else f"\n  ... and {len(missing) - 20} more"
    raise SystemExit(f"Missing {len(missing)} selected image file(s):\n  {preview}{suffix}")

output_path.write_text(
    "".join(f"{path}\n" for path in sorted(resolved_paths)),
    encoding="utf-8",
)
PY

mv -f "$sql_output" "$TRANSFER_DIR/auction_artworks.sql"
mv -f "$rsync_output" "$TRANSFER_DIR/rsync-files.txt"

image_count="$(grep -c '^' "$TRANSFER_DIR/rsync-files.txt" || true)"
sql_size="$(du -h "$TRANSFER_DIR/auction_artworks.sql" | awk '{print $1}')"
echo "Created:" >&2
echo "  $TRANSFER_DIR/auction_artworks.sql ($sql_size)" >&2
echo "  $TRANSFER_DIR/rsync-files.txt ($image_count image paths)" >&2
echo >&2
echo "Transfer examples (run from this repository root):" >&2
echo "  rsync -a --files-from=\"$TRANSFER_DIR/rsync-files.txt\" ./ user@host:/path/to/smARTmatch/" >&2
echo "  rsync -a \"$TRANSFER_DIR/auction_artworks.sql\" user@host:/path/to/smARTmatch/transfer/" >&2
