# Review frontend operations

The `frontend` Compose service is the match-review web application. It reads artwork, scores, statistics, and visualization data from PostgreSQL and writes reviewer actions such as ratings/bookmarks. Root Compose publishes container port `80` on host port `80`.

## Setup

Required settings:

| Variable | Purpose | Compose value |
|---|---|---|
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Database connection | shared DB settings |
| `SMARTMATCH_IMAGES_DIR` | Approved artwork image root | `/app/db/images` |
| `CACHE_DIR` | Approved match-visualization root | `/app/cache` |
| `FRONTEND_HOST` | Listen address in container | `0.0.0.0` |
| `FRONTEND_PORT` | Listen port in container | `80` |
| `SMARTMATCH_MATCH_EXPIRATION_AGE` | Age after which matches appear under `Abgelaufen`; positive duration using `s`, `m`, `h`, or `d` | `30d` |
| `SMARTMATCH_STATS_CACHE_TTL_SECONDS` | Dashboard statistics cache lifetime | `60` |

```bash
docker compose up -d --build db frontend
docker compose ps frontend
docker compose logs -f --tail=200 frontend
```

Open <http://localhost/>. The container serves the Flask application with Waitress.

## Security

The application has no built-in TLS or user authentication and can modify review data. Do not publish it directly to the internet. Put it behind an authenticated TLS reverse proxy/VPN and restrict direct access to port `80`. Keep database credentials outside Git.

DB-backed file routes serve only regular files whose resolved paths remain under `SMARTMATCH_IMAGES_DIR` or `CACHE_DIR`; paths outside those approved roots, including escaping symlinks, return 404.

## Persistent state

PostgreSQL is authoritative for reviews and scores. Artwork images come from the read-only `db/images/` bind mount; match visualizations may resolve through the read-only mounted `cache/` tree. Back up PostgreSQL and images. Pipeline caches are normally regenerable, but deleting a visualization file can break an existing frontend image link.

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

Frontend code is copied into the image at build time and is not bind-mounted at runtime. Rebuild and recreate the service to deploy reviewed code changes; only images and cache are mounted read-only, while the dedicated log mount remains writable.
