# Combined matching pipeline

`matching_pipeline` contains every deployed matching stage and the shared runtime used by the `matching_pipeline` Compose service.

```text
image_blocking -> image_matching -> metadata_extraction/normalization -> metadata_matching -> image_cleanup
```

## Layout

| Path | Responsibility |
|---|---|
| `image_blocking/` | DINOv3 embeddings and top-k candidate generation |
| `image_matching/` | SuperPoint/LightGlue verification and image-score persistence |
| `metadata_extraction/` | Description selection, LLM extraction, and the extraction/normalization coordinator |
| `metadata_normalization/` | Artist, dating, dimensions, material, and technique normalization |
| `metadata_matching/` | Metadata similarity/confidence scoring and persistence |
| `image_cleanup/` | Physical removal of fully processed unmatched auction images |
| `shared/` | Database/environment helpers, artifacts, LLM runtime, setup, and reference data |
| `Dockerfile` | Deployed combined pipeline image |
| `requirements.txt` | Pinned combined runtime dependencies |


## Setup

The service requires:

- a reachable PostgreSQL database via `POSTGRES_*`;
- source images mounted at `SMARTMATCH_IMAGES_DIR`;
- a writable `CACHE_DIR`;
- `HF_TOKEN` access to the configured `DINOV3_MODEL_ID`; and
- CUDA/NVIDIA Container Toolkit for the default DINOv3 and vLLM profile.

Metadata inference is configured with `METADATA_BACKEND`, `METADATA_MODEL`, `METADATA_QUANTIZATION`, and `METADATA_DEVICE`. For vLLM, `METADATA_GPU_MEMORY_UTILIZATION` sets the fraction of total GPU memory available to each engine and `METADATA_MAX_NUM_SEQS` caps concurrently scheduled sequences. `MATCHING_BATCH_SIZE` limits selected auction artworks/descriptions per cycle.

```bash
docker compose up -d --build db matching_pipeline telemetry
docker compose ps matching_pipeline
docker compose logs -f --tail=200 matching_pipeline
```

## Stage commands

The scheduler runs these commands sequentially once per minute:

```bash
python -m matching_pipeline.image_blocking --no-compile
python -m matching_pipeline.image_matching
python -m matching_pipeline.metadata_extraction
python -m matching_pipeline.metadata_matching
python -m matching_pipeline.image_cleanup --apply
```

`metadata_extraction` also invokes `metadata_normalization` using the generated JSONL handoff. Each handoff record carries parse status: malformed or unparseable LLM responses remain extraction-pending for a later retry, while a valid expected JSON schema is complete even when all entity values are empty. The normalization package can be run directly for recovery with `python -m matching_pipeline.metadata_normalization` after a valid handoff exists.

Stop the scheduler before manual writer runs. Only run image matching after successful blocking, and restart scheduled operation afterwards.

## Persistent state and maintenance

| State | Default host location | Authority |
|---|---|---|
| PostgreSQL scores and processed flags | `smartmatch_pgdata` volume | Authoritative; back up |
| Source images | `db/images/` | Lost-artwork and matched-auction images are retained; back up |
| Blocking and feature caches | `cache/matching_pipeline/` | Regenerable |
| Downloaded models | `cache/matching_pipeline/models/` | Regenerable |

Cache paths and schemas did not change during package consolidation. Stop the service before invalidating caches. Cache deletion does not reset database processed flags or perform historical reprocessing.

Monitor `cycle=... finished` log lines, stage tracebacks, disk use, pending-row trends, nonzero image `failed_images`/`failed_pairs`, and cleanup summaries. Failed image work remains pending rather than becoming deletion-eligible.

The scheduler writes an atomic work-status heartbeat consumed by the Compose
healthcheck. Active stages and a successful latest cycle are healthy. Any failed
stage marks the service unhealthy for the rest of that cycle and between cycles;
only a later fully successful cycle restores health. Missing, malformed, or
older-than-three-minute status is unhealthy. This changes Compose health status
only—the scheduler keeps its existing retry cadence and Compose does not restart
an unhealthy container automatically. Inspect it with:

```bash
docker compose ps matching_pipeline
docker inspect --format '{{json .State.Health}}' "$(docker compose ps -q matching_pipeline)"
```

See [auction image cleanup](image_cleanup/README.md) for its dry-run command,
eligibility rules, direct-deletion safeguards, and legacy-data warning.

## Optional telemetry service

The dedicated `telemetry` Compose service is independent of `matching_pipeline`, so repeated matcher failures or restarts cannot block its schedule. Set `TELEMETRY_ENABLED=true`, an absolute HTTPS `TELEMETRY_ENDPOINT`, and a long random `TELEMETRY_AUTH_TOKEN` matching the receiver secret to create one logical sync when the telemetry container starts and once per UTC day. For isolated local testing only, `TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP=true` permits HTTP endpoints whose hostname is loopback, a private IP, or a local Docker DNS name; public HTTP endpoints remain rejected. Each trigger creates one `sync_id`. Inventory pages first send only IDs and row hashes for every `match_score` relation and its matched lost/auction artworks. The receiver replies per page with missing or stale IDs; data pages then send only those requested full rows as deduplicated entity dictionaries. Each sync runs in an isolated worker process with a two-hour hard deadline. Every telemetry-container start schedules a fresh `startup` sync; if its worker cannot complete, the daemon retries it every 30 seconds until it succeeds or the container stops. Daemon state, snapshot/hash stages, page spooling, send/acknowledgement progress, retries, and terminal failures all use the shared container logging adapter; monitor them with `docker compose logs -f telemetry` or the mounted `logs/telemetry_YYYY-MM-DD.txt` file.

Local two-stack testing configuration is intentionally kept in the independent
`local/telemetry-receiver` repository. Its `scripts/run_local_telemetry_stacks.sh`
creates the isolated bridge and starts both projects with receiver-owned Compose
overrides; no local endpoint or test token is stored in this sender stack.

Before enabling telemetry on an existing database, apply the coordination-table migration:

```bash
scripts/apply_production_migration.sh \
  db/init-production/migrations/16_add_telemetry_daily_attempt.sql \
  db/init-production/migrations/17_add_image_file_source_url.sql \
  db/init-production/migrations/18_add_telemetry_sync_pagination.sql \
  db/init-production/migrations/19_make_image_file_path_unique.sql \
  db/init-production/migrations/20_track_error_free_image_matching.sql \
  db/init-production/migrations/21_mark_cleaned_up_image_files.sql
```

Code-fixed limits cap every inventory and data page at 5 MiB compressed and 20 MiB uncompressed. Both phases paginate independently from one repeatable-read database snapshot. Pagination is an operation boundary, not truncation: each phase's final page declares `complete=true`, `truncated=false`, the ordered page hashes, and the operation hash. The sender validates every receiver response and retries failed page requests up to three times. `TELEMETRY_TIMEOUT_SECONDS` defaults to 30 and redirects are rejected.

The first page also contains aggregate counts, deterministic database hashes, and image-tree hashes based only on relative paths and file sizes, plus immediate image-subdirectory hashes, Git identity, and runtime versions. Image files are stat-ed but never opened or read for telemetry hashing. Entity pages contain complete `lost_artwork`, `auction_artwork`, and `match_score` rows plus referenced artists, locations, institutions, literature, auction parties, matching programs, image files, and image links. Image bytes, GPU embeddings, feature caches, and database credentials remain excluded. New scraper downloads persist `image_file.source_url`, allowing the receiver to download missing image files. Apply migrations 17 and 19 before deploying the updated ORM/scraper code; historical images with no source URL require a controlled rescrape or backfill.

See [image blocking](image_blocking/README.md) and [image matching](image_matching/README.md) for stage-specific recovery.

## Tests

```bash
python -m compileall -q matching_pipeline scripts/run_pipeline_scheduler.py
python -m pytest -q tests/matching_pipeline
python -m pytest -q
```
