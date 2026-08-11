# Image blocking operations

Blocking uses DINOv3 embeddings to produce ranked lost-image candidates for image matching. It reads image links from PostgreSQL, reuses lost-image embeddings, writes Parquet/NPZ artifacts under `${CACHE_DIR}/image_blocking`, and marks successfully embedded image files.

## Setup

Follow [../README.md](../README.md). Blocking specifically requires:

- readable files below `SMARTMATCH_IMAGES_DIR`;
- writable `CACHE_DIR`;
- valid `POSTGRES_*` settings;
- `HF_TOKEN` access to an explicit `DINOV3_MODEL_ID`; and
- CUDA, unless `ALLOW_NON_GPU_INFERENCE=1` permits a slower fallback.

Normal scheduler command:

```bash
python -m matching_pipeline.image_blocking --no-compile
```

`MATCHING_BATCH_SIZE` limits auction artworks per run. Important CLI controls are `--top-k` (default `100`), `--image-batch-size` (default `1`), `--auction-limit`, `--lost-limit`, and `--clear-candidates`.

## Safe manual operation

Stop the shared scheduler before writing artifacts:

```bash
docker compose stop matching_pipeline
docker compose run --rm --no-deps matching_pipeline \
  python -m matching_pipeline.image_blocking --no-compile --clear-candidates
docker compose up -d matching_pipeline
```

Useful diagnostics:

```bash
# Export selected DB input without inference
docker compose run --rm --no-deps matching_pipeline \
  python -m matching_pipeline.image_blocking --only-write-input-csv

# Show full CLI
docker compose run --rm --no-deps matching_pipeline \
  python -m matching_pipeline.image_blocking --help
```

Do not use `--include-processed-auction-images` without an explicit `--auction-limit`.

## Cache and recovery

Main paths:

```text
${CACHE_DIR}/image_blocking/lost/embeddings.npz
${CACHE_DIR}/image_blocking/{lost,auction}/image_files.parquet
${CACHE_DIR}/image_blocking/auction_to_lost_candidates/part-*.parquet
```

Stop `matching_pipeline` before invalidation:

| Change/problem | Remove |
|---|---|
| Corrupt/stale candidate shard or changed auction bytes | `auction_to_lost_candidates/` |
| Changed lost image bytes/path | lost `embeddings.npz`, candidates, and matching `sp_feats/` for affected IDs |
| Changed DINO model/revision/processor | lost `embeddings.npz` and all candidates |

Candidate filenames do not encode image bytes or model revision, so semantic changes require manual invalidation. Files are replaced atomically, but a complete run and its DB updates are not one transaction. Cache deletion does not reset processed flags.

Monitor blocking counts/duration, cache disk usage, missing/out-of-root files, Hugging Face 401/403 errors, CUDA/OOM failures, and scheduler `failed` lines. A slow first run is expected while models and lost embeddings are created.
