# Database operations

PostgreSQL 16 is the authoritative store for source records, pipeline state, scores, and reviews. Root Compose exposes it as service `db` on host port `5434` and stores data in the named volume `smartmatch_pgdata`.

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
./scripts/backup.sh "local/db-backups/smartmatch_${timestamp}"
```

The script pauses `scrapers` only when that service is running and restores its previous state when it exits. PostgreSQL, `frontend`, and `matching_pipeline` remain online. Do not run manual image imports, migrations, or restores concurrently. Each backup contains `db_dump.dump` (PostgreSQL custom format) and `db/images/`; the progress output reports the image count, image size, dump size, and total backup size.

Test restores from a separate checkout and Compose project (or on a separate host), because the restore script always targets the current checkout's Compose `db` service and `db/images/`. A restore forcefully disconnects clients, drops and recreates the configured `POSTGRES_DB`, restores the dump, and replaces the entire live image directory. All previous database objects, rows, and image files are removed. Dump ownership and privileges are not applied; restored objects use the configured `POSTGRES_USER`. PostgreSQL roles and other cluster-wide settings are not part of this backup. Never overwrite the only production copy without a tested backup.

A failure after the database is dropped can leave it absent or empty; a later image-installation failure can leave the restored database with the previous images. Keep application services stopped, fix the cause, and rerun the same restore. If cleanup reports a remaining `.images-previous.*` directory, inspect and remove that directory manually.

```bash
if docker compose stop scrapers matching_pipeline frontend &&
   ./scripts/restore.sh "local/db-backups/smartmatch_YYYYMMDD_HHMMSS"; then
  docker compose up -d scrapers matching_pipeline frontend
else
  echo "restore aborted; verify Compose service state before retrying" >&2
fi
```

## Migrations

For an existing volume, stop application writers and use the helper; it creates a custom-format dump before applying SQL:

```bash
docker compose stop scrapers matching_pipeline frontend
if ENV_FILE="${SMARTMATCH_ENV_FILE:-.env.docker}" \
     ./scripts/apply_production_migration.sh path/to/migration.sql; then
  docker compose up -d scrapers matching_pipeline frontend
else
  echo "migration failed; writers remain stopped for review" >&2
fi
```

Without an argument the helper applies its current default migration. It reads `ENV_FILE`, not `SMARTMATCH_ENV_FILE`. Set `DB_SERVICE` or `BACKUP_DIR` when needed. Each SQL file runs separately: if a later file fails, earlier files remain applied. Review and apply migrations exactly once in release order, then verify logs before restoring traffic.

The current migration, `15_add_scraper_queue_progress.sql`, adds process-shared queue counters used by the scraper dashboard. Apply it before deploying workers that use the updated `ScraperRun` model.

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

Fresh databases receive the generic column from `01_schema_production.sql`; apply `db/init-production/migrations/14_add_lost_artwork_institution_classification.sql` only to an existing database. `local/10_classify_spsg_lost_artworks.sql` contains the SPSG-specific backfill and write trigger and can be manually appended to `local/09_import_lostart_data.sql`. It is intentionally kept separate. For local UI testing, run `local/11_insert_example_institution_matches.sql` afterward.

## Monitoring and maintenance

```bash
docker compose ps db
docker compose logs --since=30m db
docker compose exec -T db sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select now(), pg_database_size(current_database());"'
docker system df -v
```

Monitor volume capacity, connection failures, long-running queries, and backup success. Restrict host port `5434`; it is published by the development Compose file. Keep credentials out of Git and rotate them with PostgreSQL role commands, not by editing environment values alone.

`docker compose restart db` is normally safe. `docker compose down` keeps the volume; **`docker compose down -v` permanently removes it**.

## Image records

Image bytes live in `db/images/`; `image_file.file_path` points to them through auction/lost link tables. Relative paths resolve under `SMARTMATCH_IMAGES_DIR`. Preserve DB rows and image files as one logical backup.

Legacy import/backfill scripts read the repository-root `.env` and run on the host, so configure a host-reachable DB address/port before using them. `load_lost_artworks_to_prod.py` also requires all `NON_PROD_POSTGRES_*` values. Inspect `--help` and dry-run before any write.
