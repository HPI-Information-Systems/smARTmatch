# Auction image cleanup

This stage physically removes source image files for auction artworks that have completed image matching and metadata matching without producing a match.

After an eligible file is deleted—or confirmed already missing—the associated `image_file` rows are retained but updated with `file_path = NULL` and `cleaned_up_at = now()`. Cleanup does not update or delete `auction_artwork`, image-link, lost-artwork, or `match_score` rows.

## Eligibility and protection

An auction artwork is eligible only when:

- image matching, metadata extraction/normalization, and metadata matching are marked complete;
- every linked auction image is marked image-matching complete, embedded, and completed without a technical error; and
- no `match_score` row exists for the auction artwork.

Any `match_score` row protects every image of that auction artwork, regardless of match modality or human rating. A physical target is also protected when any resolved-path alias is linked to a lost artwork, to an incomplete or matched auction artwork, or directly from `match_score.best_image_file_id`.

Apply mode first takes the image store's exclusive filesystem lock (or safely skips while any coordinated writer holds it), then skips if any tracked scraper is running and locks scraper startup plus the relevant PostgreSQL tables while it rechecks all image usage, unlinks files, and marks their rows cleaned up. All first-party scraper entrypoints hold a shared writer lock for their complete run. Paths must resolve canonically beneath `SMARTMATCH_IMAGES_DIR`; symlinks, out-of-root targets, directories, and special files are rejected. Eligible missing files are marked cleaned up idempotently.

## Commands

Preview without deleting:

```bash
python -m matching_pipeline.image_cleanup
```

Apply deletion:

```bash
python -m matching_pipeline.image_cleanup --apply
```

The combined scheduler runs apply mode after metadata matching. Stop the scheduler before a manual apply run. Back up PostgreSQL and the image directory together before the first deployment.

Apply migrations `20_track_error_free_image_matching.sql` and `21_mark_cleaned_up_image_files.sql` before enabling cleanup. They add the error-free completion marker, reset historical scoreless auction images for one corrected matching pass, make `file_path` nullable, and add `cleaned_up_at`. Cleanup fails closed if either migration is absent. New image-matching failures remain pending and are not cleanup-eligible.
