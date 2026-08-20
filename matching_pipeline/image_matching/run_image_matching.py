"""Run LightGlue verification over file-backed blocking candidates."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from itertools import chain
from pathlib import Path
from time import perf_counter

import torch

from matching_pipeline.image_matching.config import (
    matching_image_resize_workers_from_env,
)
from matching_pipeline.image_matching.models import (
    DEFAULT_IMAGE_RESIZE,
    FeatureExtractor,
    FeatureMatcher,
    MatchClassifier,
    PreparedImage,
    configure_parallel_image_resize,
    prepare_image,
)
from matching_pipeline.shared.env import env_auction_to_lost_rankings_dir, env_cache_dir
from matching_pipeline.shared.gpu_memory import log_cuda_memory_best_effort
from matching_pipeline.image_matching.results import (
    AcceptedImageMatch,
    ImageMatchingRunResult,
)
from matching_pipeline.image_matching.utils import save_matches_to_csv
from matching_pipeline.image_matching.visualization import (
    build_keypoint_match_visualization,
)
from matching_pipeline.shared.artifacts import (
    RankingArtifactSummary,
    load_auction_to_lost_rankings_with_paths,
    summarize_auction_to_lost_rankings,
)

_DEFAULT_FEATS_DIR = object()
_UNSET_REVISION = object()
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

    candidate_items = iter(load_auction_to_lost_rankings_with_paths())
    try:
        first_item = next(candidate_items)
    except StopIteration as exc:
        raise ValueError(
            "Candidate artifact summary reported rows, but no ranking rows were readable"
        ) from exc
    _require_candidate_content_identity(first_item)

    resize_workers = matching_image_resize_workers_from_env()
    extractor = FeatureExtractor()
    matcher = FeatureMatcher()
    classifier = MatchClassifier()
    configure_parallel_image_resize()
    resize_executor = ThreadPoolExecutor(
        max_workers=resize_workers,
        thread_name_prefix="matching-image-resize",
    )
    try:
        logger.info(
            "Matching models initialized: extractor_device=%s matcher_device=%s "
            "resize_workers=%d resize_long_edge=%d",
            extractor.device,
            matcher.device,
            resize_workers,
            DEFAULT_IMAGE_RESIZE,
        )

        progress = _MatchingProgress(
            total_auctions=summary["auction_file_count"],
            total_pairs=summary["row_count"],
            interval_seconds=_PROGRESS_INTERVAL_SECONDS,
        )
        processed_file_ids: list[str] = []
        found_matches: list[AcceptedImageMatch] = []
        lost_content_revision: object = _UNSET_REVISION
        auction_content_versions: dict[str, int | None] = {}
        pairs_processed = 0
        failed_images = 0
        failed_pairs = 0

        for item in chain((first_item,), candidate_items):
            _require_candidate_content_identity(item)
            auction_file_id = item["auction_file_id"]
            item_lost_revision = item["lost_content_revision"]
            if lost_content_revision is _UNSET_REVISION:
                lost_content_revision = item_lost_revision
            elif lost_content_revision != item_lost_revision:
                raise ValueError(
                    "Candidate artifacts contain inconsistent lost-content revisions"
                )
            item_auction_version = item["auction_content_version"]
            previous_version = auction_content_versions.setdefault(
                auction_file_id, item_auction_version
            )
            if previous_version != item_auction_version:
                raise ValueError(
                    "Candidate artifacts contain inconsistent auction content versions "
                    f"for image_file_id={auction_file_id}"
                )
            auction_file_path = Path(item["auction_file_path"])
            auction_image_future: Future[PreparedImage] = resize_executor.submit(
                prepare_image,
                auction_file_path,
                resize=DEFAULT_IMAGE_RESIZE,
            )
            candidates = item["match_candidates"]
            candidate_needs_preparation: list[bool] = []
            for candidate in candidates:
                if effective_feats_dir:
                    cache_base = (
                        effective_feats_dir / candidate["lost_file_id"]
                    ).with_suffix(".pt")
                    cache_exists = (
                        extractor.has_compatible_feature_cache(
                            cache_base,
                            Path(candidate["lost_file_path"]),
                        )
                        is True
                    )
                else:
                    cache_exists = False
                candidate_needs_preparation.append(not cache_exists)

            candidate_image_futures: dict[int, Future[PreparedImage]] = {}
            next_candidate_to_prepare = 0

            def fill_candidate_preparation_queue() -> None:
                nonlocal next_candidate_to_prepare
                while len(
                    candidate_image_futures
                ) < resize_workers and next_candidate_to_prepare < len(candidates):
                    index = next_candidate_to_prepare
                    next_candidate_to_prepare += 1
                    if not candidate_needs_preparation[index]:
                        continue
                    candidate_image_futures[index] = resize_executor.submit(
                        prepare_image,
                        Path(candidates[index]["lost_file_path"]),
                        resize=DEFAULT_IMAGE_RESIZE,
                    )

            fill_candidate_preparation_queue()
            try:
                feats0 = extractor.extract_prepared(auction_image_future.result())
            except torch.cuda.OutOfMemoryError:
                log_cuda_memory_best_effort(
                    logger,
                    context=f"OOM extracting auction image {auction_file_id}",
                    device=extractor.device,
                )
                logger.exception("CUDA OOM; aborting image-matching stage")
                raise
            except Exception:
                failed_images += 1
                logger.exception(
                    "Auction image failed; leaving pending auction_file_id=%s path=%s candidates=%d",
                    auction_file_id,
                    auction_file_path,
                    len(item["match_candidates"]),
                )
                for future in candidate_image_futures.values():
                    future.cancel()
                progress.maybe_log(
                    auctions_processed=len(processed_file_ids),
                    pairs_processed=pairs_processed,
                    matches_found=len(found_matches),
                    failed_images=failed_images,
                    failed_pairs=failed_pairs,
                )
                continue

            auction_had_failed_pair = False
            for candidate_index, candidate in enumerate(candidates):
                prepared_image_future = candidate_image_futures.pop(
                    candidate_index,
                    None,
                )
                fill_candidate_preparation_queue()
                lost_file_id = candidate["lost_file_id"]
                lost_file_path = Path(candidate["lost_file_path"])
                try:
                    if effective_feats_dir:
                        feats1_path = (effective_feats_dir / lost_file_id).with_suffix(
                            ".pt"
                        )
                        feats1 = extractor.load_or_extract(
                            feats1_path,
                            lost_file_path,
                            save_missing_feats=save_missing_feats,
                            prepared_image=(
                                None
                                if prepared_image_future is None
                                else prepared_image_future.result
                            ),
                        )
                    else:
                        if prepared_image_future is None:
                            raise RuntimeError(
                                "Missing prepared image for uncached candidate "
                                f"{lost_file_id}"
                            )
                        feats1 = extractor.extract_prepared(
                            prepared_image_future.result()
                        )
                    matches01 = matcher.match(feats0, feats1)
                    prediction, confidence = classifier.classify_matches(matches01)
                    keypoint_matches = None
                    if prediction:
                        keypoint_matches = build_keypoint_match_visualization(
                            feats0,
                            feats1,
                            matches01,
                        )
                except torch.cuda.OutOfMemoryError:
                    log_cuda_memory_best_effort(
                        logger,
                        context=(
                            "OOM matching pair "
                            f"auction_file_id={auction_file_id} "
                            f"lost_file_id={lost_file_id}"
                        ),
                        device=matcher.device,
                    )
                    logger.exception("CUDA OOM; aborting image-matching stage")
                    raise
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
            lost_content_revision=(
                None
                if lost_content_revision is _UNSET_REVISION
                else lost_content_revision
            ),
            auction_content_versions=auction_content_versions,
        )
    finally:
        resize_executor.shutdown(wait=True, cancel_futures=True)


def _require_candidate_content_identity(item: Mapping[str, object]) -> None:
    if item.get("lost_content_revision") is None:
        raise ValueError(
            "Candidate artifacts are missing the lost-image content revision; "
            "rerun DB-backed image blocking"
        )
    if item.get("auction_content_version") is None:
        raise ValueError(
            "Candidate artifacts are missing an auction content version; "
            "rerun DB-backed image blocking"
        )


def _candidate_artifact_summary() -> RankingArtifactSummary:
    logger.info(
        "Loading candidate artifact summary from %s", env_auction_to_lost_rankings_dir()
    )
    summary = summarize_auction_to_lost_rankings()
    logger.info(
        "Candidate artifact summary: parts=%d rows=%d auction_groups=%d",
        summary["part_count"],
        summary["row_count"],
        summary["auction_file_count"],
    )
    return summary


def _write_results(
    results_csv: Path | None, found_matches: list[AcceptedImageMatch]
) -> None:
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
    def __init__(
        self, *, total_auctions: int, total_pairs: int, interval_seconds: float
    ) -> None:
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
