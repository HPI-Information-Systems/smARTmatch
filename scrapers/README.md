# Scraper service operations

The `scrapers` Compose service downloads auction data and images. Supervisor keeps two processes alive: the scraper dashboard and an interval scheduler. Each scheduled batch starts Christie’s, Sotheby’s, Drouot, Lot-Tissimo, and Dorotheum workers; Lost Art is manual only.

## Setup

The service requires a healthy database, writable image storage, and outbound web access.

| Variable | Purpose | Typical value |
|---|---|---|
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Database connection | Compose DB settings |
| `SMARTMATCH_IMAGES_DIR` | Download destination inside container | `/app/db/images` |
| `SCRAPER_INTERVAL` | Positive integer plus `s`, `m`, `h`, or `d` | `1d` |
| `SCRAPER_REQUEST_COOLDOWN_SECONDS` | Delay between requests | `5` or higher |
| `SMARTMATCH_DOROTHEUM_COOKIE_HEADER` / `DOROTHEUM_COOKIE_HEADER` | Optional site cookie | Secret, when required |
| `LOSTART_COOKIE_HEADER`, `LOSTART_ANUBIS_COOKIE_VERIFICATION`, `LOSTART_ANUBIS_AUTH` | Optional Lost Art access values | Secrets, when required |

Start the service from the repository root:

```bash
docker compose up -d --build db scrapers
docker compose ps scrapers
docker compose logs -f --tail=200 scrapers
```

The scheduler submits a batch immediately at startup and then at `SCRAPER_INTERVAL`. Changing environment configuration requires recreating the service: `docker compose up -d --force-recreate scrapers`.

## Operation

Dashboard: <http://localhost:5555/>. It can launch runs and change the request cooldown for future worker processes. The dashboard has no built-in authentication; do not expose it directly to the internet. Its cooldown override is stored under `/tmp` and resets when the container is recreated.

```bash
# Supervisor status
docker compose exec scrapers \
  supervisorctl -c /app/scrapers/supervisord.conf status

# Run one provider manually
docker compose exec scrapers \
  python -m scrapers.worker run christies --source cli

# Run Lost Art explicitly
docker compose exec scrapers \
  python -m scrapers.worker run lostart --source cli
```

PostgreSQL advisory locks prevent duplicate runs of the same provider. A manual trigger may therefore report that an already-running provider was skipped. Scheduled batches may overlap in time, but each provider remains single-instance.

## Persistent state and backup

- PostgreSQL stores scraped records, run history, and processing state.
- `db/images/` stores downloaded images through the `/app/db/images` bind mount.
- Browser/runtime files inside the container are disposable.

Back up PostgreSQL and `db/images/` together. Check image filesystem permissions and free space regularly; both downstream pipelines require the DB paths and files to stay synchronized.

## Monitoring and recovery

Watch `docker compose logs scrapers` for worker exit codes, HTTP throttling/blocks, parse failures, database errors, and missing image files. The Compose healthcheck verifies that dashboard and scheduler are running, not that every provider succeeds.

For a stuck service:

```bash
docker compose restart scrapers
docker compose logs --since=15m scrapers
```

Increase the cooldown when a provider rate-limits requests. Site markup or access-control changes normally require a scraper code update and image rebuild. Do not purge provider data or delete images without a database/image backup.

The dashboard-specific notes are in [../scraper_dashboard/README.md](../scraper_dashboard/README.md).
