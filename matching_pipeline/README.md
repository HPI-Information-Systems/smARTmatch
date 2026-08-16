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
docker compose up -d --build db matching_pipeline
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

`metadata_extraction` also invokes `metadata_normalization` using the generated JSONL handoff. The normalization package can be run directly for recovery with `python -m matching_pipeline.metadata_normalization` after a valid handoff exists.

Stop the scheduler before manual writer runs. Only run image matching after successful blocking, and restart scheduled operation afterwards.

## Persistent state and maintenance

| State | Default host location | Authority |
|---|---|---|
| PostgreSQL scores and processed flags | `smartmatch_pgdata` volume | Authoritative; back up |
| Source images | `db/images/` | Lost-artwork and matched-auction images are retained; back up |
| Blocking and feature caches | `cache/matching_pipeline/` | Regenerable |
| Downloaded models | `cache/matching_pipeline/models/` | Regenerable |

Cache paths and schemas did not change during package consolidation. Stop the service before invalidating caches. Cache deletion does not reset database processed flags or perform historical reprocessing.

Monitor `cycle=... finished` log lines, stage tracebacks, disk use, pending-row trends, nonzero image `failed_images`/`failed_pairs`, and cleanup summaries. Failed image work remains pending rather than becoming deletion-eligible. A running container does not prove each stage succeeded. See [auction image cleanup](image_cleanup/README.md) for its dry-run command, eligibility rules, direct-deletion safeguards, and legacy-data warning.

See [image blocking](image_blocking/README.md) and [image matching](image_matching/README.md) for stage-specific recovery.

## Tests

```bash
python -m compileall -q matching_pipeline scripts/run_pipeline_scheduler.py
python -m pytest -q tests/matching_pipeline
python -m pytest -q
```
