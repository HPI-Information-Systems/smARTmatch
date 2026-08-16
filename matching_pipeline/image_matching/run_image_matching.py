"""Run LightGlue verification over file-backed blocking candidates."""

from __future__ import annotations

import logging
from pathlib import Path
from time import perf_counter

from matching_pipeline.shared.env import env_auction_to_lost_rankings_dir, env_cache_dir
from matching_pipeline.image_matching.models import (
    FeatureExtractor,
    FeatureMatcher,
    MatchClassifier,
)
from matching_pipeline.image_matching.results import AcceptedImageMatch, ImageMatchingRunResult
from matching_pipeline.image_matching.utils import save_matches_to_csv
from matching_pipeline.image_matching.visualization import build_keypoint_match_visualization
from matching_pipeline.shared.artifacts import (
    load_auction_to_lost_rankings_with_paths,
    summarize_auction_to_lost_rankings,
)

_DEFAULT_FEATS_DIR = object()
_PROGRESS_INTERVAL_SECONDS = 90.0

logger = logging.getLogger(__name__)


# run matching on all auction image_file_id values from the blocking cache
# returns: structured accepted-match and processed-file-id information
def run_image_matching(
    results_csv: Path | None = None,
    feats_dir: Path | None | object = _DEFAULT_FEATS_DIR,
    save_missing_feats: bool = True,
) -> ImageMatchingRunResult:
    """Run matching over grouped auction-to-lost candidate artifacts."""

    run_started_at = perf_counter()
    effective_feats_dir = (
        env_cache_dir() / "sp_feats" if feats_dir is _DEFAULT_FEATS_DIR else feats_dir
    )
    assert effective_feats_dir is None or isinstance(effective_feats_dir, Path)
    results_path = _display_path(results_csv)
    feats_path = _display_path(effective_feats_dir)
    logger.info(
        "Starting LightGlue matching: candidates=%s results=%s feats=%s",
        env_auction_to_lost_rankings_dir(),
        results_path or "disabled",
        feats_path or "disabled",
    )
    summary = _candidate_artifact_summary()
    if summary["row_count"] == 0 or summary["auction_file_count"] == 0:
        logger.info(
            "No candidate rows found; skipping LightGlue model initialization and matching"
        )
        if results_csv is not None:
            logger.info("Result CSV not written because there are no candidates")
        logger.info(
            "LightGlue matching skipped in %s: no candidate rows",
            _format_duration(perf_counter() - run_started_at),
        )
        return ImageMatchingRunResult(
            processed_auction_file_ids=[],
            accepted_matches=[],
            pairs_processed=0,
            failed_images=0,
            failed_pairs=0,
        )

    extractor = FeatureExtractor()
    matcher = FeatureMatcher()
    classifier = MatchClassifier()
    logger.info(
        "Matching models initialized: extractor_device=%s matcher_device=%s",
        extractor.device,
        matcher.device,
    )

    progress = _MatchingProgress(
        total_auctions=summary["auction_file_count"],
        total_pairs=summary["row_count"],
        interval_seconds=_PROGRESS_INTERVAL_SECONDS,
    )
    processed_file_ids: list[str] = []
    found_matches: list[AcceptedImageMatch] = []
    pairs_processed = 0
    failed_images = 0
    failed_pairs = 0

    for item in load_auction_to_lost_rankings_with_paths():
        auction_file_id = item["auction_file_id"]
        auction_file_path = Path(item["auction_file_path"])
        try:
            feats0 = extractor.extract(auction_file_path)
        except Exception:
            failed_images += 1
            logger.exception(
                "Auction image failed; leaving pending auction_file_id=%s path=%s candidates=%d",
                auction_file_id,
                auction_file_path,
                len(item["match_candidates"]),
            )
            progress.maybe_log(
                auctions_processed=len(processed_file_ids),
                pairs_processed=pairs_processed,
                matches_found=len(found_matches),
                failed_images=failed_images,
                failed_pairs=failed_pairs,
            )
            continue

        auction_had_failed_pair = False
        for candidate in item["match_candidates"]:
            lost_file_id = candidate["lost_file_id"]
            lost_file_path = Path(candidate["lost_file_path"])
            try:
                if effective_feats_dir:
                    feats1_path = (effective_feats_dir / lost_file_id).with_suffix(".pt")
                    feats1 = extractor.load_or_extract(
                        feats1_path,
                        lost_file_path,
                        save_missing_feats=save_missing_feats,
                    )
                else:
                    feats1 = extractor.extract(lost_file_path)
                matches01 = matcher.match(feats0, feats1)
                prediction, confidence = classifier.classify_matches(matches01)
                keypoint_matches = None
                if prediction:
                    keypoint_matches = build_keypoint_match_visualization(
                        feats0,
                        feats1,
                        matches01,
                    )
            except Exception:
                failed_pairs += 1
                auction_had_failed_pair = True
                logger.exception(
                    "Pair failed; leaving auction image pending auction_file_id=%s auction_path=%s lost_file_id=%s lost_path=%s",
                    auction_file_id,
                    auction_file_path,
                    lost_file_id,
                    lost_file_path,
                )
            else:
                if prediction:
                    found_matches.append(
                        AcceptedImageMatch(
                            auction_file_id=auction_file_id,
                            auction_file_path=str(auction_file_path),
                            lost_file_id=lost_file_id,
                            lost_file_path=str(lost_file_path),
                            confidence=float(confidence),
                            blocking_score=float(candidate["blocking_score"]),
                            keypoint_matches=keypoint_matches,
                        )
                    )
            finally:
                pairs_processed += 1
                progress.maybe_log(
                    auctions_processed=len(processed_file_ids),
                    pairs_processed=pairs_processed,
                    matches_found=len(found_matches),
                    failed_images=failed_images,
                    failed_pairs=failed_pairs,
                )
        if not auction_had_failed_pair:
            processed_file_ids.append(auction_file_id)

    logger.info("Found %d accepted matches", len(found_matches))
    _write_results(results_csv, found_matches)

    elapsed = perf_counter() - run_started_at
    avg_pair = elapsed / pairs_processed if pairs_processed else None
    logger.info(
        "LightGlue matching finished in %s: auctions=%d/%s pairs=%d/%s accepted=%d failed_images=%d failed_pairs=%d avg_pair=%s results=%s",
        _format_duration(elapsed),
        len(processed_file_ids),
        _count_text(summary["auction_file_count"]),
        pairs_processed,
        _count_text(summary["row_count"]),
        len(found_matches),
        failed_images,
        failed_pairs,
        f"{avg_pair:.2f}s" if avg_pair is not None else "n/a",
        results_path or "disabled",
    )

    return ImageMatchingRunResult(
        processed_auction_file_ids=processed_file_ids,
        accepted_matches=found_matches,
        pairs_processed=pairs_processed,
        failed_images=failed_images,
        failed_pairs=failed_pairs,
    )


def _candidate_artifact_summary() -> dict[str, int]:
    logger.info("Loading candidate artifact summary from %s", env_auction_to_lost_rankings_dir())
    summary = summarize_auction_to_lost_rankings()
    logger.info(
        "Candidate artifact summary: parts=%d rows=%d auction_groups=%d",
        summary["part_count"],
        summary["row_count"],
        summary["auction_file_count"],
    )
    return summary


def _write_results(results_csv: Path | None, found_matches: list[AcceptedImageMatch]) -> None:
    if results_csv is None:
        logger.info("Result CSV writing disabled")
        return
    started_at = perf_counter()
    logger.info("Writing %d accepted matches to %s", len(found_matches), results_csv)
    save_matches_to_csv(found_matches, results_csv)
    logger.info(
        "Results CSV written in %.1fs: %s",
        perf_counter() - started_at,
        results_csv,
    )


class _MatchingProgress:
    def __init__(self, *, total_auctions: int, total_pairs: int, interval_seconds: float) -> None:
        self.total_auctions = total_auctions
        self.total_pairs = total_pairs
        self.interval_seconds = interval_seconds
        self.started_at = perf_counter()
        self.last_logged_at = self.started_at

    def maybe_log(
        self,
        *,
        auctions_processed: int,
        pairs_processed: int,
        matches_found: int,
        failed_images: int,
        failed_pairs: int,
    ) -> None:
        now = perf_counter()
        if now - self.last_logged_at < self.interval_seconds:
            return
        elapsed = now - self.started_at
        avg_pair = elapsed / pairs_processed if pairs_processed else None
        eta = _eta_text(self.total_pairs, pairs_processed, avg_pair)
        logger.info(
            "Matching progress: auctions=%d/%s pairs=%d/%s matches=%d failed_images=%d failed_pairs=%d avg_pair=%s eta=%s elapsed=%s",
            auctions_processed,
            _count_text(self.total_auctions),
            pairs_processed,
            _count_text(self.total_pairs),
            matches_found,
            failed_images,
            failed_pairs,
            f"{avg_pair:.2f}s" if avg_pair is not None else "n/a",
            eta,
            _format_duration(elapsed),
        )
        self.last_logged_at = now


def _eta_text(total_pairs: int, pairs_processed: int, avg_pair: float | None) -> str:
    if total_pairs <= 0 or pairs_processed <= 0 or avg_pair is None:
        return "unknown"
    remaining = max(total_pairs - pairs_processed, 0)
    return _format_duration(remaining * avg_pair)


def _count_text(value: int) -> str:
    return str(value) if value > 0 else "unknown"


def _display_path(path: Path | None) -> str | None:
    if path is None:
        return None
    path = Path(path).expanduser()
    if path.is_absolute():
        return str(path)
    return str((Path.cwd() / path).resolve())


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{sec:02d}s"
