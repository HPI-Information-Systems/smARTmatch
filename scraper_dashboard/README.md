# Scraper dashboard operations

The dashboard is the control and status UI embedded in the `scrapers` Compose service; it is not deployed as a separate container. It reads scraper runs and storage statistics from PostgreSQL and can start workers or change their request cooldown.

## Setup and access

Follow [../scrapers/README.md](../scrapers/README.md), then open <http://localhost:5555/>. Supervisor runs it with Waitress on container port `5555`.

The dashboard uses the scraper service's `POSTGRES_*` settings and image bind mount. It has **no built-in authentication or TLS** and includes write/control actions, so put it behind authenticated access controls or restrict port `5555` to a trusted network.

## Maintenance

```bash
docker compose ps scrapers
docker compose exec scrapers \
  supervisorctl -c /app/scrapers/supervisord.conf status scraper-dashboard
docker compose logs --since=30m scrapers
```

Storage statistics are cached for 60 seconds. A cooldown changed in the UI affects future workers but is stored in `/tmp/smartmatch-scraper-runtime.json`; it is lost when the container is recreated. Put the durable default in `SCRAPER_REQUEST_COOLDOWN_SECONDS`.

If the UI is available but shows stale/empty status, check database connectivity and scraper logs. Restart only the dashboard when possible:

```bash
docker compose exec scrapers supervisorctl \
  -c /app/scrapers/supervisord.conf restart scraper-dashboard
```

Restarting the whole `scrapers` service also restarts the scheduler and immediately submits a new scraper batch.
