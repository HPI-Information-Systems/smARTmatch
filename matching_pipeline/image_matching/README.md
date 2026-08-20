# Image matching operations

Image matching reads the candidate shards produced by [blocking](../image_blocking/README.md), verifies pairs with SuperPoint/LightGlue and the bundled classifier, and persists accepted scores, visualizations, and processed flags to PostgreSQL.

## Setup and run

Use the shared `matching_pipeline` setup in [../README.md](../README.md). Matching requires the same database, image, and cache mounts as blocking. Its normal scheduler command is:

```bash
python -m matching_pipeline.image_matching
```

For a one-off run, stop the scheduler first and run matching only if blocking succeeds. An explicit blocking run with `--include-processed-auction-images` atomically resets image completion state under the shared storage lock before reading files, so those replay inputs cannot become cleanup-eligible between blocking and persistence:

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

`MATCHING_IMAGE_RESIZE_WORKERS` controls the bounded CPU thread pool used to
decode and resize source images to a 720-pixel long edge before CUDA transfer
(default: up to 4 workers). OpenCV performs this work in native code outside
the Python GIL; OpenCV's own inner thread count is limited to one to avoid CPU
oversubscription. SuperPoint extracts at most 1,024 keypoints per image.

Before loading SuperPoint and LightGlue onto CUDA, the stage logs free, total,
allocated, and reserved GPU memory and rejects model transfers that cannot fit.
A CUDA OOM during image extraction or pair matching is fatal to the stage so
subsequent candidates do not repeat the same allocation failure.

## Maintenance

- PostgreSQL `match_score` and processed flags are authoritative. When an image upsert leaves a written pair without a metadata score, the same transaction resets that artwork's metadata-matching flag and timestamp so the metadata stage visits the pair.
- `${CACHE_DIR}/sp_feats/<lost_file_id>.<identity_sha256>.pt` caches lost-image SuperPoint features. The identity covers a SHA-256 fingerprint of the source bytes plus the cache schema, effective SuperPoint configuration and model-state hash, pinned LightGlue revision, feature dependency versions, CPU/OpenCV resize and coordinate-restoration policy, and CPU/GPU device type.
- Cache payloads repeat that identity metadata and include a canonical feature-tensor digest. Metadata, required tensor keys, shapes, finite values, and the tensor digest are validated on read. Legacy, stale, malformed, corrupt, or interrupted entries are ignored; missing entries are regenerated and atomically installed without exposing partial files.
- Content-addressed entries from old source/model identities are not reused. They may be removed as orphaned maintenance data while the scheduler is stopped.
- Back up PostgreSQL and source images before classifier/model upgrades or any reprocessing.

Monitor the persisted summary in `matching_pipeline` logs. Auction images with failed feature extraction or any failed candidate comparison remain pending, so later cycles retry them and filesystem cleanup cannot mistake a technical failure for a classifier rejection. Cache removal alone does not reset database state or remove old accepted scores. Before enabling cleanup on an existing database, apply `20_track_error_free_image_matching.sql`; it resets historical scoreless links for one corrected matching pass and adds the error-free completion marker required by cleanup.
