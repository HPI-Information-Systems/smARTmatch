# Review frontend operations

The `frontend` Compose service is the match-review web application. It reads artwork, scores, statistics, and visualization data from PostgreSQL and writes reviewer actions such as ratings/bookmarks. Root Compose publishes container port `80` on host port `80`.

## Setup

Required settings:

| Variable | Purpose | Compose value |
|---|---|---|
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Database connection | shared DB settings |
| `SMARTMATCH_IMAGES_DIR` | Artwork image root | `/app/db/images` |
| `FRONTEND_HOST` | Listen address in container | `0.0.0.0` |
| `FRONTEND_PORT` | Listen port in container | `80` |
| `FRONTEND_DEBUG` | Flask debug mode | `0` |
| `SMARTMATCH_MATCH_EXPIRATION_AGE` | Age after which matches appear under `Abgelaufen`; positive duration using `s`, `m`, `h`, or `d` | `30d` |
| `SMARTMATCH_STATS_CACHE_TTL_SECONDS` | Dashboard statistics cache lifetime | `60` |

```bash
docker compose up -d --build db frontend
docker compose ps frontend
docker compose logs -f --tail=200 frontend
```

Open <http://localhost/>. Keep `FRONTEND_DEBUG=0` outside development. The current container uses Flask's built-in server (`app.run`), not a production WSGI server; keep traffic/concurrency limited or deploy a tested production WSGI configuration before broader use.

## Security

The application has no built-in TLS or user authentication and can modify review data. Do not publish it directly to the internet. Put it behind an authenticated TLS reverse proxy/VPN and restrict direct access to port `80`. Keep database credentials outside Git.

## Persistent state

PostgreSQL is authoritative for reviews and scores. Artwork images come from the `db/images/` bind mount; match visualizations may resolve through the mounted `cache/` tree. Back up PostgreSQL and images. Pipeline caches are normally regenerable, but deleting a visualization file can break an existing frontend image link.

## Monitoring and recovery

```bash
docker compose logs --since=30m frontend
docker compose exec -T db sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1"'
docker compose restart frontend
```

Check database connectivity, HTTP errors, image-file permissions/paths, and free space. A 404 for an image usually means the DB `image_file.file_path` no longer resolves under `SMARTMATCH_IMAGES_DIR` or the corresponding bind-mounted file is missing.

Rebuild/recreate the service after application or dependency changes:

```bash
docker compose up -d --build frontend
```

The source directory is bind-mounted by the development Compose file, but production updates should still use a reviewed, immutable image rather than relying on live host edits.
