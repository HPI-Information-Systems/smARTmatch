# Telemetry sender

The top-level `telemetry` package implements the optional sender used by the independent Compose `telemetry` service. It schedules startup and UTC-daily synchronization, builds bounded summaries and inventories, and selectively replicates records requested by the configured receiver.

The telemetry container is included in the default Compose stack, but telemetry delivery is disabled by default. With `TELEMETRY_ENABLED=false`, the daemon stays healthy and idle: it does not launch synchronization workers, query PostgreSQL, or make telemetry HTTP requests. Keeping the container present avoids a second Compose profile or enablement variable that could drift from `TELEMETRY_ENABLED`.

## Export scope

> **Enabling telemetry authorizes inventory of the complete historical match graph.**
>
> The seven-day window in summary metadata applies only to recent summary counts. It does **not** limit synchronization to records created or updated in the last seven days.

Every synchronization inventories every `match_score` relation currently in PostgreSQL, together with the paired lost- and auction-artwork identifiers and deterministic row hashes. There is no match-date predicate on this inventory. A receiver with no prior state can therefore request full rows for the complete historical graph; later runs normally request only missing or stale rows.

Requested data pages can contain:

- complete `match_score`, `lost_artwork`, and `auction_artwork` rows;
- referenced artists, locations, institutions, literature, auction parties, and matching programs;
- image-file metadata and artwork/image link rows, including available `source_url` values;
- aggregate database counts, deterministic dataset hashes, and the latest successfully applied migration ledger entry;
- baked build Git identity and copied-source hash, pinned dependency versions from component requirement locks, and matching artifact hashes;
- image-tree metadata based on relative paths and file sizes.

The sender does not transmit image bytes, GPU embeddings, feature caches, model files, database credentials, or the telemetry bearer token. Image files are stat-ed but not opened for telemetry hashing, and the Compose service mounts `db/images` read-only.

## Package layout

`telemetry.py` and `telemetry_sync.py` remain stable compatibility entry points. The implementation is split by responsibility:

- `config.py`, `models.py`, and `constants.py` define sender settings, value objects, and fixed limits.
- `delivery.py`, `summary.py`, `database.py`, and `serialization.py` orchestrate attempts and build the aggregate summary.
- `tree_hashing.py` and `provenance.py` collect bounded filesystem and reproducibility metadata.
- `daemon.py` owns scheduling and process lifecycle; `cli.py` owns argument parsing and one-shot worker execution.
- `sync_models.py`, `sync_errors.py`, and `sync_constants.py` define the synchronization contract.
- `sync_budget.py`, `sync_workspace.py`, and `sync_catalog.py` enforce resource limits and persist operation-local state.
- `sync_inventory.py`, `sync_queries.py`, `sync_graph.py`, and `sync_data.py` inventory and materialize receiver-selected closures.
- `sync_codec.py`, `sync_http.py`, and `sync_delivery.py` encode, transmit, acknowledge, and coordinate each operation.
- `build_provenance.py` creates the immutable Git/source artifact inside the Docker build.
- `Dockerfile` and `requirements.txt` define the dedicated lightweight service image and its runtime dependencies.

## Container dependencies and requirement inspection

The telemetry image installs only `telemetry/requirements.txt`, currently the PostgreSQL driver. It does not install the root application lock or the matching pipeline's CUDA, Torch, vLLM, image-processing, or model-serving dependencies.

For reproducibility reporting, the Dockerfile copies the root `requirements.txt` and `matching_pipeline/requirements.txt` into the image as inspection inputs. The sender hashes each complete lock file and parses all exact `==` pins into `reproducibility.requirement_locks`; selected matching-package versions are also exposed through `reproducibility.packages`. These versions describe the declared component locks, not distributions installed in the telemetry container.

The image also copies matching source and small matching artifacts because telemetry reports their deterministic hashes. Copying those files does not install their dependency lock.

### Baked image provenance

The Docker build creates `/app/telemetry-build-provenance.json` in a throwaway
provenance stage. That stage resolves and validates the object ID referenced by
`HEAD` from Git metadata in the build context, then hashes the paths, modes, and
contents of every copied source/lock file. This source hash captures dirty
working-tree content even when the Git commit itself is unchanged.

The Docker context includes only `HEAD`, loose references, and packed references.
The object database and repository configuration, history, logs, hooks, and
index remain excluded. Raw reference files stay in the throwaway provenance
stage; the final image receives only the generated provenance artifact and does
not install Git. A checkout whose `.git` file points outside the build context,
such as a linked worktree, must be built from a standalone checkout.

Build normally with Docker Compose:

```bash
docker compose build telemetry
# or: docker compose up -d --build
```

A missing, invalid, or unresolvable `HEAD` fails the provenance stage instead
of baking an ambiguous image.

At runtime, telemetry requires the artifact named by
`SMARTMATCH_BUILD_PROVENANCE_FILE`, validates its exact schema, and recomputes
the copied-source snapshot before reporting it. A missing, malformed, oversized,
or inconsistent artifact is a terminal telemetry configuration failure.
Advancing or modifying the host checkout after an image build cannot change the
reported commit or source hash; rebuilding the image is required.

The container starts the daemon with:

```bash
python -m telemetry.telemetry --daemon
```

The daemon launches isolated one-shot workers using the same module with `--trigger startup` or `--trigger daily`.

## Synchronization flow

1. The sender collects non-database metadata and opens a repeatable-read database snapshot.
2. It builds the summary and spools inventory pages for the complete historical match graph. Inventory pages contain identifiers and hashes rather than complete entity rows.
3. Each inventory page is posted to the receiver. The receiver returns the missing or stale match/entity identifiers it needs.
4. The inventory snapshot and database connection are closed before network delivery continues.
5. A second repeatable-read snapshot loads the requested rows and their referenced entity closure. Current row hashes must still match the advertised inventory.
6. Data pages are fully spooled and validated before the first data page is sent. The sender then posts pages and validates every acknowledgement.
7. The daily result is recorded in `telemetry_daily_attempt` with the synchronization ID, page totals, status, hashes, byte counts, and failure classification.

A synchronization is selective after inventory, but the inventory itself always covers all historical matches. The first inventory page's summary includes `reproducibility.database.latest_applied_migration` with the ledger application order, migration filename, SHA-256 checksum, and application timestamp. It is `null` when `public.schema_migrations` does not exist or has no applied row.

## Scheduling and failure behavior

The dedicated Compose service is independent from the matching scheduler. When enabled, it attempts one synchronization when the telemetry container starts and one per UTC day. Each synchronization runs in a child process with a cooperative two-hour deadline.

Transient startup failures use capped exponential backoff for at most five attempts. Protocol/configuration errors, ordinary 4xx responses, quota violations, and unsendable closures are terminal for that trigger. HTTP retries are limited to transport failures, 408, 425, 429, and 5xx responses; bounded `Retry-After` values are honored for 429 and 503. Redirects are rejected.

The daemon writes an atomic status heartbeat for the Compose healthcheck.
Intentional disablement, active work, and pending bounded startup retries are
healthy (`disabled`, `running`, or `degraded`). Terminal startup failure,
exhausted startup retries, and failed daily work are unhealthy until a later
worker succeeds. Missing, malformed, or older-than-three-minute status also
fails closed. Health status is observational: the daemon retains its existing
retry schedule and Compose does not restart a container solely because it is
unhealthy.

## Resource bounds

Code-level limits bound the sender independently of environment configuration:

- 5 MiB compressed and 20 MiB uncompressed per page;
- 2 GiB total local synchronization workspace;
- 4 GiB aggregate compressed transfer;
- 4,096 pages per operation;
- 256 MiB filesystem free-space reserve;
- disk-backed inventory/selection catalogs instead of operation-sized Python collections.

Inventory pagination loads only match identifiers first. Before complete rows used for inventory hashes or requested data, artwork links, image metadata, or referenced entities are returned to the worker, size-only database probes debit a per-page materialization budget tied to remaining workspace capacity. Oversized batches are split before their full closures are loaded; a single closure that cannot fit in one bounded page is rejected before materialization and before any data page is posted.

## Configuration and security

Configure the selected Compose environment file:

```dotenv
TELEMETRY_ENABLED=false
TELEMETRY_ENDPOINT=
TELEMETRY_AUTH_TOKEN=
TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP=false
TELEMETRY_TIMEOUT_SECONDS=30
TELEMETRY_PAGE_DELAY_MIN_SECONDS=0.25
TELEMETRY_PAGE_DELAY_MAX_SECONDS=0.5
```

Production endpoints must use HTTPS under `smartmatch.leogruetzner.com`. Use a long random bearer token shared with the receiver. The insecure-local option is only for isolated loopback, private-IP, or local Docker DNS testing; it permits HTTP only for those local targets. Successful page acknowledgements are followed by a random delay in the configured inclusive range, reducing sustained request bursts through Cloudflare. Transient retry backoff and bounded `Retry-After` handling still apply independently.

Before enabling telemetry on an existing database, apply migrations 16 through 22 from `db/init-production/migrations/`. New scraper image URLs require migrations 17 and 19 for source mapping and unique file paths.

## Operations

Follow service logs and inspect work health with:

```bash
docker compose logs -f telemetry
docker compose ps telemetry
docker inspect --format '{{json .State.Health}}' "$(docker compose ps -q telemetry)"
```

The shared logging adapter also writes daily `logs/telemetry_YYYY-MM-DD.txt` files. Stop the telemetry service together with all other database clients before migrations or restores.

Run the focused tests with:

```bash
python -m pytest -q tests/telemetry tests/test_telemetry_schema.py
```
