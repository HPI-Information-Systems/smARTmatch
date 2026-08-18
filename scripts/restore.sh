#!/usr/bin/env bash
# Restore a backup created by scripts/backup.sh into the current Compose stack.
set -euo pipefail

usage() {
    echo "Usage: $0 BACKUP_PATH"
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

print_docker_compose_command() {
    printf '  docker compose --project-directory %q' "$ROOT_DIR"
    printf ' %q' "$@"
    printf '\n'
}

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
    usage
    exit 0
fi
[[ $# -eq 1 ]] || { usage >&2; exit 2; }
[[ -n "$1" ]] || fail "BACKUP_PATH must not be empty"
command -v docker >/dev/null 2>&1 || fail "docker is required"
[[ -d "$1" ]] || fail "backup directory not found: $1"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DB_DIR="$ROOT_DIR/db"
BACKUP_PATH="$(cd "$1" && pwd -P)"
DUMP_PATH="$BACKUP_PATH/db_dump.dump"
BACKUP_IMAGES="$BACKUP_PATH/db/images"
TARGET_IMAGES="$DB_DIR/images"

[[ -s "$DUMP_PATH" ]] || fail "backup dump not found or empty: $DUMP_PATH"
[[ -d "$BACKUP_IMAGES" ]] || fail "backup image directory not found: $BACKUP_IMAGES"
if [[ -e "$TARGET_IMAGES" || -L "$TARGET_IMAGES" ]]; then
    [[ -d "$TARGET_IMAGES" ]] || fail "live image path is not a directory: $TARGET_IMAGES"
    target_images_real="$(cd "$TARGET_IMAGES" && pwd -P)"
    [[ "$(cd "$BACKUP_IMAGES" && pwd -P)" != "$target_images_real" ]] || \
        fail "BACKUP_PATH points at the live repository data"
    case "$BACKUP_PATH/" in
        "$target_images_real/"*) fail "BACKUP_PATH must not be inside $target_images_real" ;;
    esac
fi

LOCK_PARENT="$ROOT_DIR/backups"
LOCK_DIR="$LOCK_PARENT/.data-maintenance.lock"
staged_dump=""
staged_images=""
previous_images_dir=""
application_services=(scrapers matching_pipeline telemetry frontend)
cleanup_restore() {
    local status=$?
    trap - EXIT

    if [[ -n "$previous_images_dir" && \
          ( -e "$previous_images_dir/images" || -L "$previous_images_dir/images" ) && \
          ! -e "$TARGET_IMAGES" && ! -L "$TARGET_IMAGES" ]]; then
        if ! mv -- "$previous_images_dir/images" "$TARGET_IMAGES"; then
            echo "Error: previous images remain at $previous_images_dir/images" >&2
        fi
    fi
    if [[ -n "$staged_dump" && ( -e "$staged_dump" || -L "$staged_dump" ) ]]; then
        rm -f -- "$staged_dump" || \
            echo "Error: staged dump remains at $staged_dump" >&2
    fi
    if [[ -n "$staged_images" && ( -e "$staged_images" || -L "$staged_images" ) ]]; then
        rm -rf -- "$staged_images" || \
            echo "Error: staged images remain at $staged_images" >&2
    fi
    if [[ -n "$previous_images_dir" && -d "$previous_images_dir" && \
          ! -e "$previous_images_dir/images" && ! -L "$previous_images_dir/images" ]]; then
        rm -rf -- "$previous_images_dir" || \
            echo "Error: previous-image staging directory remains at $previous_images_dir" >&2
    fi
    rmdir "$LOCK_DIR" 2>/dev/null || true
    exit "$status"
}

mkdir -p "$LOCK_PARENT"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    fail "another backup, restore, or migration may be running; if not, remove stale lock: $LOCK_DIR"
fi
trap cleanup_restore EXIT

cd "$ROOT_DIR"
log "Checking that application services are stopped"
log "Stop application services before restoring with:"
print_docker_compose_command stop "${application_services[@]}"
for service in "${application_services[@]}"; do
    if ! running_service="$(docker compose ps --status running --quiet "$service")"; then
        fail "could not determine the state of Compose service: $service"
    fi
    [[ -z "$running_service" ]] || fail "stop Compose service before restoring: $service"
done
log "WARNING: this drops and recreates the Compose database."
log "[1/5] Staging and validating the complete PostgreSQL dump: $DUMP_PATH"
staged_dump="$(mktemp "$LOCK_PARENT/.db-dump-restore.XXXXXX")"
if ! cp -v "$DUMP_PATH" "$staged_dump"; then
    fail "could not stage PostgreSQL dump; database was not changed"
fi
if [[ "$(LC_ALL=C dd if="$staged_dump" bs=5 count=1 2>/dev/null)" != "PGDMP" ]] ||
   ! docker compose exec -T db pg_restore --file=/dev/null <"$staged_dump"; then
    fail "invalid or unreadable custom-format PostgreSQL dump"
fi

log "[2/5] Staging backup images"
staged_images="$(mktemp -d "$DB_DIR/.images-restore.XXXXXX")"
if ! cp -av "$BACKUP_IMAGES/." "$staged_images/"; then
    fail "could not stage backup images; database was not changed"
fi
if [[ -e "$TARGET_IMAGES" || -L "$TARGET_IMAGES" ]]; then
    previous_images_dir="$(mktemp -d "$DB_DIR/.images-previous.XXXXXX")"
fi

log "[3/5] Dropping, recreating, and restoring the Compose database"
if ! docker compose exec -T db sh -c '
    : "${POSTGRES_USER:?POSTGRES_USER is missing in the db container}"
    : "${POSTGRES_DB:?POSTGRES_DB is missing in the db container}"
    : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is missing in the db container}"
    case "$POSTGRES_DB" in
        template0|template1)
            echo "Refusing to replace PostgreSQL template database: $POSTGRES_DB" >&2
            exit 1
            ;;
    esac
    export PGPASSWORD="$POSTGRES_PASSWORD"

    dropdb --force --if-exists --maintenance-db=template1 \
        --username="$POSTGRES_USER" "$POSTGRES_DB" &&
    PGDATABASE=template1 createdb --template=template0 --owner="$POSTGRES_USER" \
        --username="$POSTGRES_USER" "$POSTGRES_DB" &&
    pg_restore --exit-on-error --single-transaction \
        --no-owner --no-privileges --verbose \
        --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"
' <"$staged_dump"; then
    fail "database replacement failed; live images were not changed"
fi

log "[4/5] Replacing live images with the staged backup"
if [[ -n "$previous_images_dir" ]]; then
    mv -- "$TARGET_IMAGES" "$previous_images_dir/images"
fi
if ! mv -- "$staged_images" "$TARGET_IMAGES"; then
    if [[ -n "$previous_images_dir" && \
          ( -e "$previous_images_dir/images" || -L "$previous_images_dir/images" ) ]]; then
        mv -- "$previous_images_dir/images" "$TARGET_IMAGES" || true
    fi
    fail "could not install the staged images after the database restore"
fi
staged_images=""

if [[ -n "$previous_images_dir" ]]; then
    log "Removing the previous live image directory"
    rm -rf -- "$previous_images_dir" || \
        fail "restore succeeded, but the previous image directory remains at $previous_images_dir"
    previous_images_dir=""
fi
rm -f -- "$staged_dump"
staged_dump=""
rmdir "$LOCK_DIR"
trap - EXIT

log "[5/5] Restore complete"
printf 'Restored database and images from: %s\n' "$BACKUP_PATH"
printf 'Start application services with:\n'
print_docker_compose_command up -d "${application_services[@]}"
