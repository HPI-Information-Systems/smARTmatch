#!/usr/bin/env bash
# Print only the latest successfully applied production migration filename.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env.docker}"
DB_SERVICE="${DB_SERVICE:-db}"

usage() {
  cat <<'EOF'
Usage: ./scripts/latest_applied_migration.sh

Prints the latest successfully applied migration filename from
public.schema_migrations. Exits nonzero if the ledger or an applied row is
missing.
EOF
}

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
  usage
  exit 0
fi
if [[ $# -ne 0 ]]; then
  usage >&2
  exit 2
fi

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

run_psql() {
  docker compose exec -T -e PGPASSWORD="$POSTGRES_PASSWORD" "$DB_SERVICE" \
    psql -X -qAt -v ON_ERROR_STOP=1 \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"
}

has_ledger="$(run_psql -c \
  "SELECT to_regclass('public.schema_migrations') IS NOT NULL;")"
if [[ "$has_ledger" != "t" ]]; then
  echo "No migration ledger found. Baseline verified migrations first." >&2
  exit 1
fi

latest="$(run_psql -c "
SELECT migration_name
FROM public.schema_migrations
WHERE status = 'applied'
ORDER BY application_order DESC
LIMIT 1;")"
if [[ -z "$latest" ]]; then
  echo "No applied migrations are recorded." >&2
  exit 1
fi

printf '%s\n' "$latest"
