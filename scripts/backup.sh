#!/usr/bin/env bash
# Back up the Docker Compose database and db/images into one directory.
set -euo pipefail

usage() {
    echo "Usage: $0 OUTPUT_PATH"
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
    usage
    exit 0
fi
[[ $# -eq 1 ]] || { usage >&2; exit 2; }
[[ -n "$1" ]] || fail "OUTPUT_PATH must not be empty"
command -v docker >/dev/null 2>&1 || fail "docker is required"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SOURCE_IMAGES="$(cd "$ROOT_DIR/db/images" && pwd -P)"

REQUESTED_BACKUP_PATH="$1"
if [[ "$REQUESTED_BACKUP_PATH" != /* ]]; then
    REQUESTED_BACKUP_PATH="$(pwd -P)/$REQUESTED_BACKUP_PATH"
fi
BACKUP_PARENT="$(dirname "$REQUESTED_BACKUP_PATH")"
BACKUP_NAME="$(basename "$REQUESTED_BACKUP_PATH")"
[[ "$BACKUP_NAME" != "." && "$BACKUP_NAME" != ".." ]] || fail "invalid OUTPUT_PATH: $1"
mkdir -p -- "$BACKUP_PARENT"
BACKUP_PARENT="$(cd "$BACKUP_PARENT" && pwd -P)"
BACKUP_PATH="$BACKUP_PARENT/$BACKUP_NAME"
DUMP_PATH="$BACKUP_PATH/db_dump.dump"
BACKUP_DB_DIR="$BACKUP_PATH/db"
BACKUP_IMAGES="$BACKUP_DB_DIR/images"

case "$BACKUP_PATH/" in
    "$SOURCE_IMAGES/"*) fail "OUTPUT_PATH must not be inside $SOURCE_IMAGES" ;;
esac
[[ "$BACKUP_IMAGES" != "$SOURCE_IMAGES" ]] || fail "OUTPUT_PATH must not be the repository root"
[[ ! -e "$BACKUP_PATH" && ! -L "$BACKUP_PATH" ]] || fail "OUTPUT_PATH already exists: $BACKUP_PATH"

LOCK_PARENT="$ROOT_DIR/local"
LOCK_DIR="$LOCK_PARENT/.data-maintenance.lock"
backup_complete=0
lock_held=0
backup_path_created=0
scrapers_was_running=0
cleanup_backup() {
    local status=$?
    trap - EXIT
    trap '' INT TERM

    if [[ $scrapers_was_running -eq 1 ]]; then
        log "Restarting the scraper service after backup exit"
        if docker compose start scrapers; then
            scrapers_was_running=0
        else
            echo "Error: could not restart the scraper service; start it manually" >&2
            status=1
        fi
    fi
    if [[ $backup_complete -eq 0 && $backup_path_created -eq 1 ]]; then
        rm -rf -- "$BACKUP_PATH" || status=1
    fi
    if [[ $lock_held -eq 1 ]] && ! rmdir "$LOCK_DIR" 2>/dev/null; then
        echo "Error: could not remove maintenance lock: $LOCK_DIR" >&2
        status=1
    fi
    exit "$status"
}

mkdir -p "$LOCK_PARENT"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    fail "another backup or restore may be running; if not, remove stale lock: $LOCK_DIR"
fi
lock_held=1
trap cleanup_backup EXIT

mkdir -p -- "$BACKUP_DB_DIR"
backup_path_created=1
trap 'exit 130' INT
trap 'exit 143' TERM

cd "$ROOT_DIR"
log "[1/5] Pausing image ingestion"
if ! running_scrapers="$(docker compose ps --status running --quiet scrapers)"; then
    fail "could not determine the scraper service state"
fi
if [[ -n "$running_scrapers" ]]; then
    scrapers_was_running=1
fi
if ! docker compose stop scrapers; then
    fail "could not stop the scraper service"
fi
if [[ $scrapers_was_running -eq 1 ]]; then
    log "Scraper service stopped; PostgreSQL, frontend, and matching pipeline remain online."
else
    log "Scraper service was not running and will remain stopped."
fi
log "Do not run manual image imports, migrations, or restores until this backup finishes."

log "[2/5] Creating PostgreSQL dump: $DUMP_PATH"
if ! docker compose exec -T db sh -c '
    : "${POSTGRES_USER:?POSTGRES_USER is missing in the db container}"
    : "${POSTGRES_DB:?POSTGRES_DB is missing in the db container}"
    : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is missing in the db container}"
    export PGPASSWORD="$POSTGRES_PASSWORD"
    exec pg_dump --format=custom --verbose \
        --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"
' >"$DUMP_PATH"; then
    fail "database dump failed"
fi
[[ -s "$DUMP_PATH" ]] || fail "database dump is empty"

log "[3/5] Copying images to: $BACKUP_IMAGES"
mkdir -p -- "$BACKUP_IMAGES"
if ! cp -av "$SOURCE_IMAGES/." "$BACKUP_IMAGES/"; then
    fail "image copy failed"
fi

log "[4/5] Calculating backup summary"
image_count="$(find "$BACKUP_IMAGES" -type f | wc -l | tr -d '[:space:]')"
image_size="$(du -sh "$BACKUP_IMAGES" | awk '{ print $1 }')"
dump_size="$(du -sh "$DUMP_PATH" | awk '{ print $1 }')"
total_size="$(du -ch "$BACKUP_IMAGES" "$DUMP_PATH" | awk 'END { print $1 }')"
backup_complete=1

if [[ $scrapers_was_running -eq 1 ]]; then
    log "[5/5] Restarting the scraper service"
    if ! docker compose start scrapers; then
        fail "backup completed at $BACKUP_PATH, but the scraper service could not be restarted; start it manually"
    fi
    scrapers_was_running=0
else
    log "[5/5] Leaving the scraper service stopped"
fi

printf '\nBackup complete: %s\n' "$BACKUP_PATH"
printf '  Image files: %s\n' "$image_count"
printf '  Image file size: %s\n' "$image_size"
printf '  Database dump size: %s\n' "$dump_size"
printf '  Total backup file size: %s\n' "$total_size"
