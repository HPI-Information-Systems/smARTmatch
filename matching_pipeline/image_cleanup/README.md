# Auction image cleanup

This stage physically removes source image files for auction artworks that have completed image matching and metadata matching without producing a match.

After an eligible file is deleted—or confirmed already missing—the associated `image_file` rows are retained but updated with `file_path = NULL` and `cleaned_up_at = now()`. Cleanup does not update or delete `auction_artwork`, image-link, lost-artwork, or `match_score` rows.

## Eligibility and protection

An auction artwork is eligible only when:

- image matching, metadata extraction/normalization, and metadata matching are marked complete;
- every linked auction image is marked image-matching complete, embedded, and completed without a technical error; and
- no `match_score` row exists for the auction artwork.

Any `match_score` row protects every image of that auction artwork, regardless of match modality or human rating. A physical target is also protected when any resolved-path alias is linked to a lost artwork, to an incomplete or matched auction artwork, or directly from `match_score.best_image_file_id`.

Apply mode first takes the image store's exclusive filesystem lock (or safely skips while any coordinated writer holds it), then skips if any tracked scraper is running and locks scraper startup plus the relevant PostgreSQL tables while it rechecks all image usage. Before changing PostgreSQL, each eligible file is durably journaled and atomically renamed into `${SMARTMATCH_IMAGES_DIR}/.smartmatch-cleanup-quarantine/`. After cleanup markers commit, quarantined bytes are purged. At the start of every apply run, journals from an interrupted run are reconciled against PostgreSQL: pre-commit moves are restored to their original paths and post-commit remnants are purged. Failures before a commit attempt are restored immediately; every commit exception retains its journal until PostgreSQL state can be reconciled on a fresh transaction. A post-commit purge failure is reported distinctly as purge-pending and is completed by the next apply run without pretending the database transaction rolled back. Commit ambiguity therefore leaves recoverable bytes rather than an irreversible missing source file. All first-party scraper entrypoints hold a shared writer lock for their complete run. Paths must resolve canonically beneath `SMARTMATCH_IMAGES_DIR`; symlinks, out-of-root targets, directories, special files, and the quarantine itself are rejected. Eligible files already missing before cleanup are marked cleaned up idempotently.

Do not manually move or remove quarantine files. If reconciliation reports an ambiguous or malformed journal, stop image writers, preserve the quarantine directory and PostgreSQL backup, correct the conflicting database/path state, and rerun cleanup apply mode. Valid journals are reconciled independently before cleanup fails closed on unresolved entries.

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
