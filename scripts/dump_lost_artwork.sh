#!/usr/bin/env bash
# dump_lost_artwork.sh
#
# Erzeugt einen selbst-enthaltenen SQL-Dump aller von lost_artwork abhängenden
# Tabellen sowie image_file / lost_artwork_image_file.
#
# Der Dump kann sicher auf eine bestehende DB eingespielt werden:
#   • Vorhandene Zeilen werden nicht überschrieben oder gelöscht (ON CONFLICT DO NOTHING)
#   • FK-Checks werden während des Imports temporär deaktiviert
#   • img_paths (source-DB) werden automatisch in image_file /
#     lost_artwork_image_file (prod-DB) umgewandelt
#
# Verwendung:
#   ./dump_lost_artwork.sh [output.sql]
#
# Umgebungsvariablen (Defaults für lokale Dev-DB):
#   SRC_HOST, SRC_PORT, SRC_DB, SRC_USER, SRC_PASS
#
# Einspielen:
#   psql -h <host> -U <user> -d <database> < output.sql

set -euo pipefail

SRC_HOST="${SRC_HOST:-localhost}"
SRC_PORT="${SRC_PORT:-5432}"
SRC_DB="${SRC_DB:-smartmatch}"
SRC_USER="${SRC_USER:-smartmatch}"
SRC_PASS="${SRC_PASS:-smartmatch}"
SRC_CONTAINER="${SRC_CONTAINER:-}"   # optional: Docker-Container-ID/-Name
OUTPUT="${1:-lost_artwork_export_$(date +%Y%m%d_%H%M%S).sql}"

# psql entweder lokal oder über docker exec
if [ -n "$SRC_CONTAINER" ]; then
    PSQL="docker exec -e PGPASSWORD=$SRC_PASS $SRC_CONTAINER psql -h $SRC_HOST -p $SRC_PORT -U $SRC_USER -d $SRC_DB"
elif command -v psql &>/dev/null; then
    export PGPASSWORD="$SRC_PASS"
    PSQL="psql -h $SRC_HOST -p $SRC_PORT -U $SRC_USER -d $SRC_DB"
else
    echo "Fehler: psql nicht gefunden. SRC_CONTAINER setzen oder psql installieren." >&2
    exit 1
fi

echo "→ Erstelle Dump aus $SRC_DB@$SRC_HOST:$SRC_PORT ..." >&2

# ─── Hilfsfunktionen ─────────────────────────────────────────────────────────

# Gibt die Spaltenliste einer Tabelle als kommaseparierte Zeichenkette zurück
cols() {
    local table=$1
    $PSQL -tAq -c "
        SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = '$table'"
}

# Gibt einen COPY-FROM-STDIN-Block für die übergebene SELECT-Abfrage aus
copy_block() {
    local temp_table=$1   # Name der Temp-Tabelle
    local col_list=$2     # Spaltenliste (kommasepariert, bereits gequotet)
    local query=$3        # SELECT-Abfrage

    echo "COPY _${temp_table} (${col_list}) FROM STDIN (FORMAT csv);"
    $PSQL -c "COPY ($query) TO STDOUT (FORMAT csv)"
    printf '\\.\n\n'
}

# ─── Kopfzeile ────────────────────────────────────────────────────────────────
cat > "$OUTPUT" << 'HEADER'
-- =============================================================================
-- lost_artwork data export
-- Erzeigt mit: dump_lost_artwork.sh
-- =============================================================================
-- Sicheres Einspielen: vorhandene Zeilen werden NICHT überschrieben.
--
-- Restore:
--   psql -h <host> -U <user> -d <database> < this_file.sql
-- =============================================================================

BEGIN;

-- FK-Checks temporär deaktivieren (wird am Ende der Transaktion zurückgesetzt)
SET session_replication_role = replica;

HEADER

echo "  country ..." >&2
{
    C=$(cols country)
    echo "-- ── country ─────────────────────────────────────────────────────────────────"
    echo "CREATE TEMP TABLE _country (LIKE country INCLUDING DEFAULTS) ON COMMIT DROP;"
    copy_block "country" "$C" "SELECT $C FROM country"
    echo "INSERT INTO country ($C)"
    echo "SELECT $C FROM _country"
    echo "ON CONFLICT (country_id) DO NOTHING;"
    echo
} >> "$OUTPUT"

echo "  location ..." >&2
{
    C=$(cols location)
    echo "-- ── location ────────────────────────────────────────────────────────────────"
    echo "CREATE TEMP TABLE _location (LIKE location INCLUDING DEFAULTS) ON COMMIT DROP;"
    copy_block "location" "$C" "SELECT $C FROM location"
    echo "INSERT INTO location ($C)"
    echo "SELECT $C FROM _location"
    echo "ON CONFLICT (location_id) DO NOTHING;"
    echo
} >> "$OUTPUT"

echo "  location_name_variants ..." >&2
{
    C=$(cols location_name_variants)
    echo "-- ── location_name_variants ──────────────────────────────────────────────────"
    echo "CREATE TEMP TABLE _location_name_variants (LIKE location_name_variants INCLUDING DEFAULTS) ON COMMIT DROP;"
    copy_block "location_name_variants" "$C" "SELECT $C FROM location_name_variants"
    echo "INSERT INTO location_name_variants ($C)"
    echo "SELECT $C FROM _location_name_variants"
    echo "ON CONFLICT (name_variant_id) DO NOTHING;"
    echo
} >> "$OUTPUT"

echo "  institution ..." >&2
{
    C=$(cols institution)
    echo "-- ── institution ─────────────────────────────────────────────────────────────"
    echo "CREATE TEMP TABLE _institution (LIKE institution INCLUDING DEFAULTS) ON COMMIT DROP;"
    copy_block "institution" "$C" "SELECT $C FROM institution"
    echo "INSERT INTO institution ($C)"
    echo "SELECT $C FROM _institution"
    echo "ON CONFLICT (institution_id) DO NOTHING;"
    echo
} >> "$OUTPUT"

echo "  contact ..." >&2
{
    C=$(cols contact)
    echo "-- ── contact ─────────────────────────────────────────────────────────────────"
    echo "CREATE TEMP TABLE _contact (LIKE contact INCLUDING DEFAULTS) ON COMMIT DROP;"
    copy_block "contact" "$C" "SELECT $C FROM contact"
    echo "INSERT INTO contact ($C)"
    echo "SELECT $C FROM _contact"
    echo "ON CONFLICT (contact_id) DO NOTHING;"
    echo
} >> "$OUTPUT"

echo "  literature_source ..." >&2
{
    C=$(cols literature_source)
    echo "-- ── literature_source ───────────────────────────────────────────────────────"
    echo "CREATE TEMP TABLE _literature_source (LIKE literature_source INCLUDING DEFAULTS) ON COMMIT DROP;"
    copy_block "literature_source" "$C" "SELECT $C FROM literature_source"
    echo "INSERT INTO literature_source ($C)"
    echo "SELECT $C FROM _literature_source"
    echo "ON CONFLICT (literature_id) DO NOTHING;"
    echo
} >> "$OUTPUT"

echo "  artist ..." >&2
{
    C=$(cols artist)
    echo "-- ── artist ──────────────────────────────────────────────────────────────────"
    echo "CREATE TEMP TABLE _artist (LIKE artist INCLUDING DEFAULTS) ON COMMIT DROP;"
    copy_block "artist" "$C" "SELECT $C FROM artist"
    echo "INSERT INTO artist ($C)"
    echo "SELECT $C FROM _artist"
    echo "ON CONFLICT (artist_id) DO NOTHING;"
    echo
} >> "$OUTPUT"

echo "  artist_name_variants ..." >&2
{
    C=$(cols artist_name_variants)
    echo "-- ── artist_name_variants ────────────────────────────────────────────────────"
    echo "CREATE TEMP TABLE _artist_name_variants (LIKE artist_name_variants INCLUDING DEFAULTS) ON COMMIT DROP;"
    copy_block "artist_name_variants" "$C" "SELECT $C FROM artist_name_variants"
    echo "INSERT INTO artist_name_variants ($C)"
    echo "SELECT $C FROM _artist_name_variants"
    echo "ON CONFLICT (name_variant_id) DO NOTHING;"
    echo
} >> "$OUTPUT"

echo "  dict_material ..." >&2
{
    C=$(cols dict_material)
    echo "-- ── dict_material ───────────────────────────────────────────────────────────"
    echo "-- Selbstreferenz: Eltern vor Kindern einfügen"
    echo "CREATE TEMP TABLE _dict_material (LIKE dict_material INCLUDING DEFAULTS) ON COMMIT DROP;"
    copy_block "dict_material" "$C" "SELECT $C FROM dict_material"
    echo "INSERT INTO dict_material ($C)"
    echo "SELECT $C FROM _dict_material ORDER BY material_parent NULLS FIRST"
    echo "ON CONFLICT (material_name) DO NOTHING;"
    echo
} >> "$OUTPUT"

echo "  dict_technique ..." >&2
{
    C=$(cols dict_technique)
    echo "-- ── dict_technique ──────────────────────────────────────────────────────────"
    echo "CREATE TEMP TABLE _dict_technique (LIKE dict_technique INCLUDING DEFAULTS) ON COMMIT DROP;"
    copy_block "dict_technique" "$C" "SELECT $C FROM dict_technique"
    echo "INSERT INTO dict_technique ($C)"
    echo "SELECT $C FROM _dict_technique ORDER BY technique_parent NULLS FIRST"
    echo "ON CONFLICT (technique_name) DO NOTHING;"
    echo
} >> "$OUTPUT"

echo "  lost_artwork (ohne img_paths) ..." >&2
{
    # Alle Spalten außer img_paths
    C=$($PSQL -tAq -c "
        SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'lost_artwork'
          AND column_name != 'img_paths'")

    echo "-- ── lost_artwork ────────────────────────────────────────────────────────────"
    echo "-- img_paths wird separat als image_file / lost_artwork_image_file migriert"
    echo "CREATE TEMP TABLE _lost_artwork AS SELECT $C FROM lost_artwork LIMIT 0;"
    copy_block "lost_artwork" "$C" "SELECT $C FROM lost_artwork"
    echo "INSERT INTO lost_artwork ($C)"
    echo "SELECT $C FROM _lost_artwork"
    echo "ON CONFLICT (lost_artwork_id) DO NOTHING;"
    echo
} >> "$OUTPUT"

echo "  img_paths → image_file / lost_artwork_image_file ..." >&2
{
    echo "-- ── image_file + lost_artwork_image_file ────────────────────────────────────"
    echo "-- img_paths aus der Source-DB werden hier in die Prod-Struktur überführt."
    echo "-- Bereits vorhandene Pfade in image_file werden NICHT doppelt eingefügt."
    echo "CREATE TEMP TABLE _img_paths ("
    echo "    lost_artwork_id uuid,"
    echo "    file_path       text"
    echo ") ON COMMIT DROP;"
    echo "COPY _img_paths (lost_artwork_id, file_path) FROM STDIN (FORMAT csv);"
    $PSQL -c "
        COPY (
            SELECT la.lost_artwork_id, unnest(la.img_paths) AS file_path
            FROM lost_artwork la
            WHERE array_length(la.img_paths, 1) > 0
        ) TO STDOUT (FORMAT csv)"
    printf '\\.\n\n'

    cat << 'IMG_SQL'
-- Neue Pfade einfügen (bestehende überspringen)
INSERT INTO image_file (file_path, is_embedded)
SELECT DISTINCT file_path, false
FROM _img_paths
WHERE file_path NOT IN (SELECT file_path FROM image_file);

-- Relationen anlegen
INSERT INTO lost_artwork_image_file (lost_artwork_id, image_file_id)
SELECT p.lost_artwork_id, imf.image_file_id
FROM _img_paths p
JOIN image_file imf ON imf.file_path = p.file_path
ON CONFLICT DO NOTHING;

IMG_SQL
} >> "$OUTPUT"

# ─── Fußzeile ─────────────────────────────────────────────────────────────────
cat >> "$OUTPUT" << 'FOOTER'
-- FK-Checks wieder aktivieren
SET session_replication_role = DEFAULT;

COMMIT;
FOOTER

[ -z "$SRC_CONTAINER" ] && unset PGPASSWORD

SIZE=$(du -sh "$OUTPUT" | cut -f1)
echo "✓ Fertig: $OUTPUT ($SIZE)" >&2
