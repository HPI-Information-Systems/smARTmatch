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

Files in `db/init-production/` initialize a **new** empty volume only. Changing `POSTGRES_*` later does not rename an existing database or rotate its role password.

## Backup and restore

Back up the database and `db/images/` as one consistent set. Stop all writers for the snapshot, then restore service only after both copies succeed:

```bash
mkdir -p local/db-backups
timestamp="$(date +%Y%m%d_%H%M%S)"
docker compose stop scrapers matching_pipeline frontend
if docker compose exec -T db sh -lc \
     'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
     > "local/db-backups/smartmatch_${timestamp}.dump" \
  && tar -C db -czf "local/db-backups/images_${timestamp}.tgz" images; then
  docker compose up -d scrapers matching_pipeline frontend
else
  echo "backup failed; writers remain stopped" >&2
fi
```

Test restores in a separate database/host. For a planned restore, stop all writers first, then use `pg_restore` with reviewed ownership and cleanup options appropriate to the target. Never overwrite the only production copy without a tested backup.

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

## Lost-artwork source classification

`lost_artwork_source_classification` assigns an auditable source category without relying on `institution_id`. The imported data contains unrelated LostArt records linked to the SPSG institution, so the view instead uses embedded SPSG provenance and SPSG identity in restricted LostArt reporter/contact fields.

```sql
SELECT source_category, count(*)
FROM lost_artwork_source_classification
GROUP BY source_category
ORDER BY source_category;

SELECT la.*, classification.source_category,
       classification.classification_evidence
FROM lost_artwork la
JOIN lost_artwork_source_classification classification
  USING (lost_artwork_id);
```

For the dump in `local/09_import_lostart_data.sql`, the expected counts are 7,504 `SPSG (internal)`, 887 `SPSG (internal and lostart)`, 2,575 `SPSG (lostart)`, and 44,627 `non-SPSG`. New databases create the view during initialization. Apply `db/init-production/migrations/14_add_lost_artwork_source_classification_view.sql` to an existing database with the migration helper described above.

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
