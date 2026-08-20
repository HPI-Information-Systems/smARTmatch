#!/usr/bin/env bash
# Restore a complete backup or standalone database dump into the Compose stack.
set -euo pipefail

usage() {
    cat <<EOF
Usage:
  $0 BACKUP_PATH
  $0 --only-db-dump DUMP_PATH

BACKUP_PATH restores db_dump.dump and db/images from a complete backup.
--only-db-dump restores one custom-format PostgreSQL dump and leaves db/images unchanged.
EOF
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

MODE="full"
if [[ ${1:-} == "--only-db-dump" ]]; then
    MODE="only-db-dump"
    shift
fi
[[ $# -eq 1 ]] || { usage >&2; exit 2; }
[[ -n "$1" ]] || fail "restore path must not be empty"
command -v docker >/dev/null 2>&1 || fail "docker is required"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
DB_DIR="$ROOT_DIR/db"
TARGET_IMAGES="$DB_DIR/images"
BACKUP_PATH=""
BACKUP_IMAGES=""

if [[ "$MODE" == "only-db-dump" ]]; then
    [[ -f "$1" && -s "$1" ]] || fail "database dump not found or empty: $1"
    dump_dir="$(cd "$(dirname -- "$1")" && pwd -P)"
    DUMP_PATH="$dump_dir/$(basename -- "$1")"
    TOTAL_STEPS=3
else
    [[ -d "$1" ]] || fail "backup directory not found: $1"
    BACKUP_PATH="$(cd "$1" && pwd -P)"
    DUMP_PATH="$BACKUP_PATH/db_dump.dump"
    BACKUP_IMAGES="$BACKUP_PATH/db/images"
    TOTAL_STEPS=5

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
if [[ "$MODE" == "only-db-dump" ]]; then
    log "Live images will not be changed."
fi
log "[1/$TOTAL_STEPS] Staging and validating the complete PostgreSQL dump: $DUMP_PATH"
staged_dump="$(mktemp "$LOCK_PARENT/.db-dump-restore.XXXXXX")"
if ! cp -v "$DUMP_PATH" "$staged_dump"; then
    fail "could not stage PostgreSQL dump; database was not changed"
fi
if [[ "$(LC_ALL=C dd if="$staged_dump" bs=5 count=1 2>/dev/null)" != "PGDMP" ]] ||
   ! docker compose exec -T db pg_restore --file=/dev/null <"$staged_dump"; then
    fail "invalid or unreadable custom-format PostgreSQL dump"
fi

if [[ "$MODE" == "full" ]]; then
    log "[2/5] Staging backup images"
    staged_images="$(mktemp -d "$DB_DIR/.images-restore.XXXXXX")"
    if ! cp -av "$BACKUP_IMAGES/." "$staged_images/"; then
        fail "could not stage backup images; database was not changed"
    fi
    if [[ -e "$TARGET_IMAGES" || -L "$TARGET_IMAGES" ]]; then
        previous_images_dir="$(mktemp -d "$DB_DIR/.images-previous.XXXXXX")"
    fi
    database_step=3
else
    database_step=2
fi

log "[$database_step/$TOTAL_STEPS] Dropping, recreating, and restoring the Compose database"
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

if [[ "$MODE" == "full" ]]; then
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
fi
rm -f -- "$staged_dump"
staged_dump=""
rmdir "$LOCK_DIR"
trap - EXIT

if [[ "$MODE" == "full" ]]; then
    log "[5/5] Restore complete"
    printf 'Restored database and images from: %s\n' "$BACKUP_PATH"
else
    log "[3/3] Database-only restore complete"
    printf 'Restored database from: %s\n' "$DUMP_PATH"
    printf 'Live images were not changed: %s\n' "$TARGET_IMAGES"
fi
printf 'Start application services with:\n'
print_docker_compose_command up -d "${application_services[@]}"
