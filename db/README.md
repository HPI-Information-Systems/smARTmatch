# Database operations

PostgreSQL 16 is the authoritative store for source records, pipeline state, scores, and reviews. Root Compose exposes it only to other Compose services as `db:5432` and stores data in the named volume `smartmatch_pgdata`.

## Setup

Set these values in the protected deployment environment before the first start:

```env
POSTGRES_DB=smartmatch_production
POSTGRES_USER=smartmatch
POSTGRES_PASSWORD=<secret>
POSTGRES_HOST=db
POSTGRES_PORT=5432
PGDATA=/var/lib/postgresql/data/pgdata
```

Then start and verify it:

```bash
docker compose up -d db
docker compose exec -T db sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1"'
```

Top-level SQL files in `db/init-production/` initialize a **new** empty volume only; PostgreSQL does not recursively execute the `migrations/` directory. The main schema in `01_schema_production.sql` includes institution classification for fresh databases, and its indexes are defined in `02_indices.sql`. Changing `POSTGRES_*` later does not rename an existing database or rotate its role password.

## Backup and restore

Back up the database and `db/images/` as one consistent set:

```bash
timestamp="$(date +%Y%m%d_%H%M%S)"
./scripts/backup.sh "backups/smartmatch_${timestamp}"
```

While holding the shared maintenance lock, the script stops `scrapers` and `matching_pipeline` if they are running so neither image ingestion nor matching cleanup can change database paths or image files between the database dump and image copy. It logs every stop and restart, restarts exactly the services that were running before the backup even when the backup fails, and leaves services that were already stopped alone. PostgreSQL, `telemetry`, and `frontend` remain online. Do not run manual image imports, migrations, or restores concurrently. Each backup contains `db_dump.dump` (PostgreSQL custom format) and `db/images/`; the progress output reports the image count, image size, dump size, and total backup size.

Test restores from a separate checkout and Compose project (or on a separate host), because the restore script always targets the current checkout's Compose `db` service and, for a complete restore, `db/images/`. A restore forcefully disconnects clients, drops and recreates the configured `POSTGRES_DB`, and restores the dump. The default mode also replaces the entire live image directory, removing all previous database objects, rows, and image files. Dump ownership and privileges are not applied; restored objects use the configured `POSTGRES_USER`. PostgreSQL roles and other cluster-wide settings are not part of this backup. Never overwrite the only production copy without a tested backup.

Migration backups created by `apply_production_migration.sh` are standalone custom-format dumps rather than complete backup directories. Restore one while leaving `db/images/` untouched with `--only-db-dump`:

```bash
docker compose stop scrapers matching_pipeline telemetry frontend
./scripts/restore.sh --only-db-dump \
  backups/smartmatch_production_YYYYMMDD_HHMMSS.dmp
```

A failure after the database is dropped can leave it absent or empty; in default mode, a later image-installation failure can leave the restored database with the previous images. Keep application services stopped, fix the cause, and rerun the same restore. If cleanup reports a remaining `.images-previous.*` directory, inspect and remove that directory manually.

```bash
if docker compose stop scrapers matching_pipeline telemetry frontend &&
   ./scripts/restore.sh "backups/smartmatch_YYYYMMDD_HHMMSS"; then
  docker compose up -d scrapers matching_pipeline telemetry frontend
else
  echo "restore aborted; verify Compose service state before retrying" >&2
fi
```

## Migrations

For an existing volume, stop application writers and use the helper; it creates a custom-format dump before applying SQL:

```bash
docker compose stop scrapers matching_pipeline telemetry frontend
if ENV_FILE="${SMARTMATCH_ENV_FILE:-.env.docker}" \
     ./scripts/apply_production_migration.sh path/to/migration.sql; then
  docker compose up -d scrapers matching_pipeline telemetry frontend
else
  echo "migration failed; writers remain stopped for review" >&2
fi
```

The helper has no default migration and requires one or more explicit migration paths. It reads `ENV_FILE`, not `SMARTMATCH_ENV_FILE`. Set `DB_SERVICE` or `BACKUP_DIR` when needed. Before creating the backup, the helper acquires `backups/.data-maintenance.lock`, shared with backup and restore operations, and holds it until the complete migration run exits. If that lock already exists, inspect the active maintenance operation or remove the lock only after confirming it is stale. Each SQL file runs separately: if a later file fails, earlier files remain applied. Review and apply migrations in release order, then verify logs before restoring traffic.

The helper records each successful migration basename, SHA-256 checksum, status, and timestamps in `public.schema_migrations`. An already-recorded migration is skipped; a changed checksum or an incomplete/failed ledger row stops the run for review. Query the latest successful entry with:

```bash
ENV_FILE="${SMARTMATCH_ENV_FILE:-.env.docker}" \
  ./scripts/latest_applied_migration.sh
```

Successful stdout contains only the migration filename, making the command suitable for deployment checks. It exits nonzero when the ledger does not exist or has no successful row.

Databases whose migrations predate the ledger require a one-time, explicit baseline. After independently verifying that each listed migration is already present, record it without executing its SQL:

```bash
ENV_FILE="${SMARTMATCH_ENV_FILE:-.env.docker}" \
  ./scripts/apply_production_migration.sh --baseline \
  db/init-production/migrations/20_track_error_free_image_matching.sql \
  db/init-production/migrations/21_mark_cleaned_up_image_files.sql
```

Baseline mode still creates a database backup. Never baseline a migration merely because its file exists; this would make the ledger disagree with the real schema.

Migration 19 collapses duplicate `image_file.file_path` rows without transferring `is_embedded=true` across image IDs. It resets each affected canonical image and dependent auction processing state so ID-keyed embedding and candidate artifacts cannot be trusted accidentally. After applying it, run image blocking successfully before running image cleanup; blocking rewrites the lost embedding cache with canonical IDs and replaces stale candidate identities.

Migration 24 adds `image_file.content_sha256`, monotonic `content_version`, and the lost-image corpus revision used to reject stale in-flight matching writes. Its triggers invalidate image-derived scores and replay state whenever a scraper records different bytes or changes lost-image link membership: lost-corpus changes schedule every live auction image, while auction-image changes schedule all live sibling images of linked artworks. Metadata-bearing match rows keep their metadata and review fields; image-only rows are removed. During the one-time legacy replay, the migration preserves existing scores while resetting embedding and image-processing state, avoiding an empty match view while the unchanged matcher catches up. Apply it before deploying scraper, blocking, or matching code that uses these identities.

## Lost-artwork institution classification

`lost_artwork.institution_classification` is a generic, optional indexed institution label; `NULL` means uncategorized. Shared schema and migrations do not contain institution-specific classification rules. Import-specific scripts may populate the column and maintain it on writes. The frontend discovers distinct labels dynamically and only compares stored values.

```sql
SELECT institution_classification, count(*)
FROM lost_artwork
GROUP BY institution_classification
ORDER BY institution_classification NULLS FIRST;

SELECT lost_artwork_id, institution_classification
FROM lost_artwork
WHERE institution_classification IS NOT NULL;
```

Fresh databases receive the generic column from `01_schema_production.sql`; apply `db/init-production/migrations/14_add_lost_artwork_institution_classification.sql` only to an existing database.

## Monitoring and maintenance

```bash
docker compose ps db
docker compose logs --since=30m db
docker compose exec -T db sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select now(), pg_database_size(current_database());"'
docker system df -v
```

Monitor volume capacity, connection failures, long-running queries, and backup success. PostgreSQL has no published host port; perform administration through `docker compose exec db`. Keep credentials out of Git and rotate them with PostgreSQL role commands, not by editing environment values alone.

`docker compose restart db` is normally safe. `docker compose down` keeps the volume; **`docker compose down -v` permanently removes it**.

## Image records

Image bytes live in `db/images/`; `image_file.file_path` points to them through auction/lost link tables. Relative paths resolve under `SMARTMATCH_IMAGES_DIR`. Scrapers persist the SHA-256 digest of the exact normalized JPEG bytes. Before updating a stable path's digest, cooperating scraper writers take a transaction-scoped PostgreSQL advisory lock for that path and rehash the currently stored file, preserving filesystem/DB update order across concurrent runs. A digest change atomically advances `content_version` and invalidates `is_embedded`, allowing blocking to commit only the version it actually read. Filenames do not need an extension: the SPSG import uses extensionless content-addressed paths, and image decoders identify approved JPEG/PNG/WebP/GIF data from file signatures. Preserve DB rows and image files as one logical backup.
