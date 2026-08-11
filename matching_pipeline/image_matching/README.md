# Image matching operations

Image matching reads the candidate shards produced by [blocking](../image_blocking/README.md), verifies pairs with SuperPoint/LightGlue and the bundled classifier, and persists accepted scores, visualizations, and processed flags to PostgreSQL.

## Setup and run

Use the shared `matching_pipeline` setup in [../README.md](../README.md). Matching requires the same database, image, and cache mounts as blocking. Its normal scheduler command is:

```bash
python -m matching_pipeline.image_matching
```

For a one-off run, stop the scheduler first and run matching only if blocking succeeds:

```bash
docker compose stop matching_pipeline
if docker compose run --rm --no-deps matching_pipeline \
     python -m matching_pipeline.image_blocking --no-compile; then
  docker compose run --rm --no-deps matching_pipeline \
    python -m matching_pipeline.image_matching
else
  echo "blocking failed; matching was not run" >&2
fi
docker compose up -d matching_pipeline
```

Set `MATCHING_WRITE_OUTPUT_CSV=1` only when a non-authoritative debug CSV is required.

## Maintenance

- PostgreSQL `match_score` and processed flags are authoritative.
- `${CACHE_DIR}/sp_feats/*.pt` caches lost-image SuperPoint features.
- Clear affected `sp_feats` while the scheduler is stopped after lost-image bytes, SuperPoint settings, feature dependencies, or CPU/GPU execution mode changes.
- Back up PostgreSQL and source images before classifier/model upgrades or any reprocessing.

Monitor the persisted summary in `matching_pipeline` logs. Nonzero `failed_images` or `failed_pairs` require manual review: the stage still finalizes those auction file IDs as processed, so normal cycles will not retry them. Cache removal alone does not reset database state or remove old accepted scores.
