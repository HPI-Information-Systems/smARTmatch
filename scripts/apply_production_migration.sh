#!/usr/bin/env bash
# Back up the Docker Compose Postgres database, then apply and record one or
# more production migrations through psql stdin.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.docker}"
DB_SERVICE="${DB_SERVICE:-db}"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"
MAINTENANCE_LOCK_DIR="${MAINTENANCE_LOCK_DIR:-$ROOT_DIR/backups/.data-maintenance.lock}"
MODE="apply"
FORCE=0

usage() {
  cat <<'EOF'
Usage:
  ./scripts/apply_production_migration.sh [--force] MIGRATION.sql [...]
  ./scripts/apply_production_migration.sh --baseline [--force] MIGRATION.sql [...]

Normal mode backs up the database, applies each migration, and records its
filename and SHA-256 checksum in public.schema_migrations.

--baseline records migrations that an operator has independently verified as
already applied. It still creates a backup, but it does not execute the SQL.

--force bypasses the guard against processing a migration while another ledger
row is still applying or failed. It does not bypass checksum validation.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --baseline)
      MODE="baseline"
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      break
      ;;
  esac
done
if [[ $# -eq 0 ]]; then
  echo "At least one migration file is required; no default migration is selected." >&2
  usage >&2
  exit 2
fi

cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 127
fi
if command -v sha256sum >/dev/null 2>&1; then
  sha256_file() {
    sha256sum "$1" | awk '{print $1}'
  }
elif command -v shasum >/dev/null 2>&1; then
  sha256_file() {
    shasum -a 256 "$1" | awk '{print $1}'
  }
else
  echo "sha256sum or shasum is required" >&2
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

seen_migration_names=""
for migration in "$@"; do
  if [[ ! -f "$migration" ]]; then
    echo "Migration not found: $migration" >&2
    exit 1
  fi
  migration_name="$(basename "$migration")"
  case "$seen_migration_names" in
    *"|$migration_name|"*)
      echo "Duplicate migration filename: $migration_name" >&2
      exit 1
      ;;
  esac
  seen_migration_names="${seen_migration_names}|${migration_name}|"
done

run_psql() {
  docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" "$DB_SERVICE" \
    psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"
}

# psql does not interpolate -v variables in SQL supplied with -c. Sending the
# SQL through stdin keeps psql variable quoting (:'name') available.
run_psql_sql() {
  local sql="$1"
  shift
  printf '%s\n' "$sql" | run_psql "$@"
}

lock_held=0
cleanup_migration_lock() {
  local status=$?
  trap - EXIT
  trap '' INT TERM

  if [[ $lock_held -eq 1 ]] && ! rmdir "$MAINTENANCE_LOCK_DIR" 2>/dev/null; then
    echo "Error: could not remove maintenance lock: $MAINTENANCE_LOCK_DIR" >&2
    status=1
  fi
  exit "$status"
}

mkdir -p "$(dirname "$MAINTENANCE_LOCK_DIR")"
if ! mkdir "$MAINTENANCE_LOCK_DIR" 2>/dev/null; then
  echo "Another backup, restore, or migration may be running; if not, remove stale lock: $MAINTENANCE_LOCK_DIR" >&2
  exit 1
fi
lock_held=1
trap cleanup_migration_lock EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$BACKUP_DIR"
timestamp="$(date +%Y%m%d_%H%M%S)"
backup_path="$BACKUP_DIR/${POSTGRES_DB}_${timestamp}.dmp"

echo "Creating backup: $backup_path"
if ! docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" "$DB_SERVICE" \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc >"$backup_path"; then
  rm -f "$backup_path"
  echo "Backup failed; no migrations were applied or recorded." >&2
  exit 1
fi

run_psql -qAt -c "
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    application_order bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    migration_name text PRIMARY KEY,
    checksum_sha256 text NOT NULL
        CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL
        CHECK (status IN ('applying', 'applied', 'failed')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    applied_at timestamptz,
    error_message text,
    CONSTRAINT ck_schema_migrations_state CHECK (
        (status = 'applying' AND applied_at IS NULL)
        OR (status = 'applied' AND applied_at IS NOT NULL)
        OR (status = 'failed' AND applied_at IS NULL)
    )
);"

for migration in "$@"; do
  migration_name="$(basename "$migration")"
  migration_checksum="$(sha256_file "$migration")"
  existing="$(run_psql_sql "
SELECT status || '|' || checksum_sha256
FROM public.schema_migrations
WHERE migration_name = :'migration_name';" \
    -qAt -v migration_name="$migration_name")"

  if [[ -n "$existing" ]]; then
    IFS='|' read -r recorded_status recorded_checksum <<<"$existing"
    if [[ "$recorded_checksum" != "$migration_checksum" ]]; then
      echo "Checksum mismatch for recorded migration: $migration_name" >&2
      echo "Recorded: $recorded_checksum" >&2
      echo "Current:  $migration_checksum" >&2
      exit 1
    fi
    if [[ "$recorded_status" == "applied" ]]; then
      echo "Already applied; skipping: $migration_name"
      continue
    fi
    if [[ "$MODE" != "baseline" ]]; then
      echo "Migration $migration_name has ledger status '$recorded_status'." >&2
      echo "Verify the database, then use --baseline only if its SQL already completed." >&2
      exit 1
    fi
  fi

  incomplete_migrations="$(run_psql_sql "
SELECT migration_name || '|' || status
FROM public.schema_migrations
WHERE status <> 'applied'
  AND migration_name <> :'migration_name'
ORDER BY application_order;" \
    -qAt -v migration_name="$migration_name")"
  if [[ -n "$incomplete_migrations" ]]; then
    if [[ $FORCE -eq 0 ]]; then
      echo "Cannot process $migration_name while other migration ledger rows are incomplete:" >&2
    else
      echo "Warning: --force is bypassing incomplete migration ledger rows before $migration_name:" >&2
    fi
    while IFS='|' read -r incomplete_name incomplete_status; do
      echo "  $incomplete_name ($incomplete_status)" >&2
    done <<<"$incomplete_migrations"
    if [[ $FORCE -eq 0 ]]; then
      echo "Resolve the incomplete migrations first, or use --force to override this guard." >&2
      exit 1
    fi
  fi

  if [[ "$MODE" == "baseline" ]]; then
    echo "Recording verified migration without executing it: $migration_name"
    recorded="$(run_psql_sql "
INSERT INTO public.schema_migrations (
    migration_name, checksum_sha256, status, applied_at
)
VALUES (
    :'migration_name', :'migration_checksum', 'applied', clock_timestamp()
)
ON CONFLICT (migration_name) DO UPDATE
SET status = 'applied',
    applied_at = clock_timestamp(),
    error_message = NULL
WHERE public.schema_migrations.checksum_sha256 = EXCLUDED.checksum_sha256
RETURNING migration_name;" \
      -qAt \
      -v migration_name="$migration_name" \
      -v migration_checksum="$migration_checksum")"
    if [[ "$recorded" != "$migration_name" ]]; then
      echo "Failed to baseline migration: $migration_name" >&2
      exit 1
    fi
    continue
  fi

  echo "Applying migration: $migration"
  run_psql_sql "
INSERT INTO public.schema_migrations (
    migration_name, checksum_sha256, status
)
VALUES (:'migration_name', :'migration_checksum', 'applying');" \
    -qAt \
    -v migration_name="$migration_name" \
    -v migration_checksum="$migration_checksum"

  if ! run_psql <"$migration"; then
    run_psql_sql "
UPDATE public.schema_migrations
SET status = 'failed',
    error_message = 'psql exited with a non-zero status'
WHERE migration_name = :'migration_name'
  AND status = 'applying';" \
      -qAt -v migration_name="$migration_name" || \
      echo "Warning: could not mark $migration_name as failed" >&2
    echo "Migration failed: $migration_name" >&2
    exit 1
  fi

  recorded="$(run_psql_sql "
UPDATE public.schema_migrations
SET status = 'applied',
    applied_at = clock_timestamp(),
    error_message = NULL
WHERE migration_name = :'migration_name'
  AND status = 'applying'
RETURNING migration_name;" \
    -qAt -v migration_name="$migration_name")"
  if [[ "$recorded" != "$migration_name" ]]; then
    echo "Migration completed, but its ledger row could not be finalized: $migration_name" >&2
    echo "Verify the database before taking further action." >&2
    exit 1
  fi
done

echo "Done. Backup kept at: $backup_path"
