#!/usr/bin/env bash
# Back up the Docker Compose Postgres database, then apply one or more SQL
# production migrations through psql stdin.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.docker}"
DB_SERVICE="${DB_SERVICE:-db}"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/local/db-backups}"
DEFAULT_MIGRATION="$ROOT_DIR/db/init-production/migrations/14_add_lost_artwork_source_classification_view.sql"

if [[ $# -eq 0 ]]; then
  set -- "$DEFAULT_MIGRATION"
fi

cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 127
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${POSTGRES_USER:?POSTGRES_USER must be set in $ENV_FILE}"
: "${POSTGRES_DB:?POSTGRES_DB must be set in $ENV_FILE}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in $ENV_FILE}"

for migration in "$@"; do
  if [[ ! -f "$migration" ]]; then
    echo "Migration not found: $migration" >&2
    exit 1
  fi
done

mkdir -p "$BACKUP_DIR"
timestamp="$(date +%Y%m%d_%H%M%S)"
backup_path="$BACKUP_DIR/${POSTGRES_DB}_${timestamp}.dmp"

echo "Creating backup: $backup_path"
if ! docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" "$DB_SERVICE" \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc >"$backup_path"; then
  rm -f "$backup_path"
  echo "Backup failed; no migrations were applied." >&2
  exit 1
fi

for migration in "$@"; do
  echo "Applying migration: $migration"
  docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" "$DB_SERVICE" \
    psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <"$migration"
done

echo "Done. Backup kept at: $backup_path"
