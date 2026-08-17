# Image blocking operations

Blocking uses DINOv3 embeddings to produce ranked lost-image candidates for image matching. It reads image links from PostgreSQL, reuses lost-image embeddings, writes Parquet/NPZ artifacts under `${CACHE_DIR}/image_blocking`, and marks successfully embedded image files.

## Setup

Follow [../README.md](../README.md). Blocking specifically requires:

- readable files below `SMARTMATCH_IMAGES_DIR`;
- writable `CACHE_DIR`;
- valid `POSTGRES_*` settings;
- `HF_TOKEN` access to an explicit `DINOV3_MODEL_ID`; and
- CUDA, unless `ALLOW_NON_GPU_INFERENCE=1` permits a slower fallback.

`DINOV3_MODEL_ID` is the sole deployed checkpoint selector; abbreviated size-key aliases are not supported. Use the full Hugging Face model ID so model loading, embedding-cache validation, and candidate identity agree.

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
| Corrupt candidate shard | `auction_to_lost_candidates/`; valid shards are regenerated on the next blocking run |
| Changed auction image path/bytes | No manual candidate removal; the shard identity changes automatically |
| Changed lost image path/bytes | No manual removal; source fingerprints invalidate affected lost embeddings and candidate shards, and matching creates a new content-addressed `sp_feats/` entry |
| Changed DINO model revision/processor without changing `DINOV3_MODEL_ID` | Lost `embeddings.npz`; remove candidates only if blocking cannot run immediately afterward |

The lost embedding cache records each source image's absolute and resolved paths, size, and SHA-256 content fingerprint; mismatched legacy or changed entries are regenerated. Candidate filenames are content-addressed with the identity schema, active DINO model ID, exact ordered lost-embedding matrix and lost-source identity, `top_k`, database content versions, and the equivalent path/content identity for each auction image. Candidate Parquet rows repeat the expected auction/lost `content_version` and exact source SHA-256. Same-index, malformed-name, or excess shards are removed before reuse. Lost sources are revalidated after candidate generation; failed fingerprinting, disagreement with a known database digest, or any source change during generation clears candidate parts rather than leaving stale rankings available. Migration 19 resets embedding and processing state for deduplicated image IDs; migration 24 adds scraper-maintained content versions and invalidates legacy embedding state. The blocking database update matches both `image_file_id` and the expected `content_version`, so a scraper commit that changes bytes cannot be marked embedded by an older blocking run. Auction rows with `is_embedded=false` are selected even when their previous image-matching link is already processed, ensuring scraper invalidation reaches the next blocking run. Parquet files are replaced atomically, but a complete run and its DB updates are not one transaction. Cache deletion does not reset processed flags.

Monitor blocking counts/duration, cache disk usage, missing/out-of-root files, Hugging Face 401/403 errors, CUDA/OOM failures, and scheduler `failed` lines. A slow first run is expected while models and lost embeddings are created.
