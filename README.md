<p align="center">
  <img src="frontend/static/logo_smartmatch.png" width="300" alt="smARTmatch logo">
</p>

<p align="center">
  Automatic matching of lost art. Bachelor project at <a href="https://hpi.de">Hasso-Plattner-Institute</a> (HPI) in cooperation with <a href="https://spsg.de">Stiftung Preußische Schlösser und Gärten Berlin-Brandenburg</a> (SPSG).
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white" alt="Python 3.12">
  <img src="https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
</p>

## Repository scope

This public repository contains the deployable smARTmatch pipeline and its operational tooling:

| Path | Responsibility |
|---|---|
| `scrapers/`, `scraper_dashboard/` | Auction ingestion, scheduling, and status UI |
| `matching_pipeline/` | Image blocking/matching and metadata extraction/normalization/matching |
| `db/init-production/` | Production schema, indexes, migrations, and integrity seed |
| `frontend/` | Review interface for persisted matches |
| `scripts/` | Pipeline scheduling and database maintenance/onboarding utilities |

The root Compose stack has four services:

```text
scrapers ──► PostgreSQL + shared images
                         │
                         ▼
 image blocking → image matching → metadata extraction/normalization → metadata matching
                         │
                         ▼
                      frontend
```

The four matching stages run sequentially in the single `matching_pipeline` container.

## Requirements

- Docker Engine with Compose v2
- A Hugging Face token with access to the configured gated DINOv3 model
- Network access for initial model downloads
- NVIDIA drivers and NVIDIA Container Toolkit

Tracked environment files contain development placeholders only. Never commit production credentials or model tokens.

## Quick start

1. Create a private runtime environment file. Set a strong, unique `POSTGRES_PASSWORD` before the database is first initialized, and add the required Hugging Face token:

```bash
cp .env.example .env.runtime
chmod 600 .env.runtime
# edit .env.runtime; set POSTGRES_PASSWORD and HF_TOKEN
export SMARTMATCH_ENV_FILE=.env.runtime
```

`.env.runtime` is ignored by Git. The development Compose file publishes its ports on all host interfaces, so restrict them with a firewall or production override.

2. Validate and start the stack:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

The base Compose service requests all available GPUs with `gpus: all`. The tracked environment uses CUDA-backed DINOv3 and vLLM metadata inference. A fresh database is initialized with a compact integrity dataset and the corresponding files under `db/images/pipeline_test_set/`.

Interfaces:

- Review frontend: <http://localhost/>
- Scraper dashboard: <http://localhost:5555/>

Follow matching logs with:

```bash
docker compose logs -f --tail=200 matching_pipeline
```

### Application logging

All Python services use the shared adapter in `shared/logging_adapter.py`. Logs
continue to reach Docker stdout/stderr and are also persisted on the host in
`./logs` as one file per service and local calendar day, for example
`matching_pipeline_2026-08-11.txt`.

Configure the policy in the selected Docker environment file:

```dotenv
SMARTMATCH_LOG_LEVEL=ALL            # ALL or ERROR
SMARTMATCH_LOG_RETENTION_DAYS=30
SMARTMATCH_LOG_DIR=/app/logs
```

`ALL` includes DEBUG and higher records; `ERROR` includes only ERROR and
CRITICAL records. Rotation and retention use each container's system timezone
(UTC unless the container runtime is configured otherwise). Files older than
the configured calendar-day window are removed automatically when a service
starts and when it first logs on a new day.

### Inference profile

The base stack is the production GPU profile: `matching_pipeline` requests all GPUs, image inference requires CUDA, and metadata inference uses vLLM with an AWQ model. Startup fails clearly when the NVIDIA runtime is unavailable rather than silently falling back to CPU.

See the [blocking operations runbook](matching_pipeline/image_blocking/README.md) before production deployment.

## Running components

Run only infrastructure and the review interfaces:

```bash
docker compose up -d db scrapers frontend
```

Run one controlled matching cycle by stopping the scheduler and chaining the deployed stages. Chaining prevents matching from consuming stale artifacts when blocking fails; always restart scheduled operation afterwards.

```bash
docker compose stop matching_pipeline
if docker compose run --rm --no-deps matching_pipeline \
     python -m matching_pipeline.image_blocking --no-compile \
  && docker compose run --rm --no-deps matching_pipeline \
     python -m matching_pipeline.image_matching \
  && docker compose run --rm --no-deps matching_pipeline \
     python -m matching_pipeline.metadata_extraction \
  && docker compose run --rm --no-deps matching_pipeline \
     python -m matching_pipeline.metadata_matching; then
  echo "manual cycle completed"
else
  echo "manual cycle failed; inspect logs" >&2
fi
docker compose up -d matching_pipeline
```

Do not run manual writer stages concurrently with the scheduler.

Run one scraper explicitly:

```bash
docker compose run --rm --no-deps scrapers \
  python -m scrapers.worker run christies --source cli
```

## Persistent state

| Host location | Data |
|---|---|
| Docker volume `smartmatch_pgdata` | PostgreSQL data (authoritative) |
| `db/images/` | Scraped/imported images (authoritative) |
| `cache/matching_pipeline/` | Model, blocking, and matching caches (regenerable) |

Back up PostgreSQL and `db/images/` before migrations or destructive maintenance. `docker compose down` preserves the database volume; `docker compose down -v` deletes it.

### Backup and restore

The backup script temporarily stops and restarts `scrapers` when it is running. PostgreSQL, the frontend, and the matching pipeline remain online:

```bash
./scripts/backup.sh "backups/smartmatch_$(date +%Y%m%d_%H%M%S)"
```

Do not run manual image imports, migrations, or restores during a backup. A restore requires all application services to be stopped:

```bash
docker compose stop scrapers matching_pipeline frontend &&
  ./scripts/restore.sh backups/smartmatch_YYYYMMDD_HHMMSS
```

Restart application services only after the restore succeeds. Restore drops and recreates the configured database and completely replaces `db/images/`. Backups contain `db_dump.dump` and `db/images/`; see [`db/README.md`](db/README.md) for operational details.

Existing volumes are not automatically migrated. See [`db/README.md`](db/README.md) and use `scripts/apply_production_migration.sh` for reviewed production migrations. The helper uses `ENV_FILE` (not `SMARTMATCH_ENV_FILE`), so point both names at the same runtime file.

## Local development and tests

Service and development dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The root file is for local development and offline tests; container builds do not install it. Each Python service installs its dedicated runtime manifest: `scrapers/requirements.txt`, `frontend/requirements.txt`, or `matching_pipeline/requirements.txt`.

For local matching runtime dependencies, also install the combined lock and pinned LightGlue revision used by the Docker image:

```bash
python -m pip install -r matching_pipeline/requirements.txt
python -m pip install --no-deps \
  'git+https://github.com/cvg/LightGlue@eb42fee2d71449efb0aa5c10549752b5d75384d8'
```

Run the offline suite:

```bash
python -m pytest -q
```

LLM-backed metadata tests are excluded by default. Run them only with a configured backend:

```bash
python -m pytest -q -m llm tests/matching_pipeline
```

## Documentation

- [Combined matching pipeline](matching_pipeline/README.md)
- [Image blocking](matching_pipeline/image_blocking/README.md)
- [Image matching](matching_pipeline/image_matching/README.md)
- [Scrapers](scrapers/README.md) and [scraper dashboard](scraper_dashboard/README.md)
- [Database and migrations](db/README.md)
- [Review frontend](frontend/README.md)

## License

[MIT](LICENSE) © Niklas Rücker, Corrie Gunawan, Leo Grützner, Simon Hubert, Julia Sinkiewicz and Caspar Sadenius
