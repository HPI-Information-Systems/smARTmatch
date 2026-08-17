"""Run file-backed DINOv3 blocking for auction images against lost images."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from matching_pipeline.shared.artifacts import write_image_files_parquet
from matching_pipeline.shared.env import env_hf_token, env_image_root
from shared.image_storage_lock import image_storage_lock

from .candidate_generation import clear_candidate_parts, write_candidate_parts
from .config import (
    AUCTION_ROLE,
    DEFAULT_CANDIDATE_SHARD_AUCTION_IMAGES,
    DEFAULT_EMBEDDING_DTYPE,
    DEFAULT_IMAGE_BATCH_SIZE,
    DEFAULT_TOP_K,
    LOST_ROLE,
    blocking_input_csv_path,
    blocking_root,
    candidate_dir,
    lost_embedding_cache_path,
    matching_batch_size_from_env,
)
from .db_updates import ExpectedImageVersion, mark_image_files_embedded
from .embedding_cache import (
    ensure_lost_embedding_cache,
    load_dino_adapter_class,
    source_identity_sha256,
)
from .input_sources import (
    ImageFileRow,
    has_unprocessed_auction_image_file_rows,
    load_db_image_file_rows,
    read_image_file_csv,
    reset_auction_image_matching_for_replay,
    write_image_file_csv,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlockingRunResult:
    cache_dir: Path
    lost_image_count: int
    auction_image_count: int
    candidate_count: int
    candidate_part_count: int
    skipped_candidate_part_count: int
    generated_lost_embedding_count: int
    embedded_image_file_count: int = 0


@dataclass(frozen=True)
class BlockingInputCsvResult:
    input_csv: Path
    lost_image_count: int
    auction_image_count: int


def create_blocking_input_csv(
    *,
    lost_limit: int | None = None,
    auction_limit: int | None = None,
    include_processed_auction_images: bool = False,
) -> BlockingInputCsvResult:
    effective_auction_limit = _effective_auction_limit(
        input_csv=None,
        auction_limit=auction_limit,
        include_processed_auction_images=include_processed_auction_images,
    )
    logger.info(
        "Creating DB-derived blocking input CSV (lost_limit=%s, auction_artwork_limit=%s, include_processed_auction_images=%s)",
        lost_limit,
        effective_auction_limit,
        include_processed_auction_images,
    )
    if include_processed_auction_images:
        _prepare_processed_image_replay()
    lost_rows, auction_rows = load_db_image_file_rows(
        lost_limit=lost_limit,
        auction_limit=effective_auction_limit,
        include_processed_auction_images=include_processed_auction_images,
        validate_files=False,
    )
    output_path = write_image_file_csv(
        blocking_input_csv_path(),
        lost_rows,
        auction_rows,
    )
    logger.info(
        "Wrote blocking input CSV: %s (lost=%d, auction=%d)",
        output_path,
        len(lost_rows),
        len(auction_rows),
    )
    return BlockingInputCsvResult(output_path, len(lost_rows), len(auction_rows))


def run_image_blocking(
    *,
    input_csv: Path | None = None,
    top_k: int = DEFAULT_TOP_K,
    image_batch_size: int = DEFAULT_IMAGE_BATCH_SIZE,
    candidate_shard_auction_images: int = DEFAULT_CANDIDATE_SHARD_AUCTION_IMAGES,
    lost_limit: int | None = None,
    auction_limit: int | None = None,
    include_processed_auction_images: bool = False,
    dtype: str = DEFAULT_EMBEDDING_DTYPE,
    no_compile: bool = False,
    hf_token: str | None = None,
    clear_candidates: bool = False,
) -> BlockingRunResult:
    run_started_at = perf_counter()
    effective_auction_limit = _effective_auction_limit(
        input_csv=input_csv,
        auction_limit=auction_limit,
        include_processed_auction_images=include_processed_auction_images,
    )
    logger.info(
        "Starting image blocking (input_csv=%s, top_k=%d, image_batch_size=%d, candidate_shard_auction_images=%d, lost_limit=%s, auction_artwork_limit=%s, include_processed_auction_images=%s, dtype=%s, clear_candidates=%s)",
        input_csv or "DB",
        top_k,
        image_batch_size,
        candidate_shard_auction_images,
        lost_limit,
        effective_auction_limit,
        include_processed_auction_images,
        dtype,
        clear_candidates,
    )
    _validate_args(
        top_k,
        image_batch_size,
        candidate_shard_auction_images,
        lost_limit,
        effective_auction_limit,
    )
    if include_processed_auction_images and input_csv is None:
        _prepare_processed_image_replay()
    if _should_skip_for_empty_db_auction_input(
        input_csv,
        include_processed_auction_images,
    ):
        _prepare_cache_dirs(clear_candidates)
        clear_candidate_parts(candidate_dir())
        logger.info(
            "No unprocessed auction image files found in DB; skipped input loading, lost embedding cache preparation, and candidate generation"
        )
        logger.info("Image blocking finished in %.1fs", perf_counter() - run_started_at)
        return BlockingRunResult(
            blocking_root(),
            0,
            0,
            0,
            0,
            0,
            0,
        )
    load_started_at = perf_counter()
    lost_rows, auction_rows = _load_inputs(
        input_csv,
        lost_limit,
        effective_auction_limit,
        include_processed_auction_images,
    )
    logger.info(
        "Loaded blocking inputs in %.1fs: lost=%d, auction=%d",
        perf_counter() - load_started_at,
        len(lost_rows),
        len(auction_rows),
    )
    _log_sample_rows("lost", lost_rows)
    _log_sample_rows("auction", auction_rows)

    _prepare_cache_dirs(clear_candidates)
    write_image_files_parquet(LOST_ROLE, lost_rows)
    write_image_files_parquet(AUCTION_ROLE, auction_rows)
    logger.info("Wrote image-file snapshot artifacts under %s", blocking_root())

    if not auction_rows:
        clear_candidate_parts(candidate_dir())
        logger.info(
            "No auction image files selected for blocking; skipped lost embedding cache preparation and candidate generation"
        )
        logger.info("Image blocking finished in %.1fs", perf_counter() - run_started_at)
        return BlockingRunResult(
            blocking_root(),
            len(lost_rows),
            0,
            0,
            0,
            0,
            0,
        )
    if not lost_rows:
        raise ValueError("No lost image files available for blocking")

    model = _ModelProvider(no_compile=no_compile, hf_token=hf_token)
    cache_started_at = perf_counter()
    logger.info("Preparing lost embedding cache at %s", lost_embedding_cache_path())
    try:
        lost_cache = ensure_lost_embedding_cache(
            lost_rows,
            cache_path=lost_embedding_cache_path(),
            model_factory=model.get,
            batch_size=image_batch_size,
            dtype=dtype,
        )
    except Exception:
        clear_candidate_parts(candidate_dir())
        raise
    logger.info(
        "Lost embedding cache ready in %.1fs: embeddings=%d, dim=%d, generated=%d",
        perf_counter() - cache_started_at,
        len(lost_cache.file_ids),
        int(lost_cache.embeddings.shape[1]),
        int(lost_cache.metadata.get("generated_count", 0)),
    )
    candidate_started_at = perf_counter()
    lost_source_identity = _candidate_lost_source_identity(lost_cache.metadata)
    lost_content_sha256 = _candidate_lost_content_sha256(
        lost_cache.metadata,
        lost_cache.file_ids,
    )
    lost_content_versions = {
        row.file_id: row.content_version for row in lost_rows
    }
    candidate_count, part_count, skipped_count = _write_candidates(
        auction_rows,
        lost_cache.file_ids,
        lost_cache.embeddings,
        model,
        top_k,
        image_batch_size,
        candidate_shard_auction_images,
        model_identity=_candidate_model_identity(lost_cache.metadata),
        lost_source_identity=lost_source_identity,
        lost_content_versions=lost_content_versions,
        lost_content_sha256=lost_content_sha256,
    )
    if source_identity_sha256(lost_rows) != lost_source_identity:
        clear_candidate_parts(candidate_dir())
        raise RuntimeError("Lost images changed during candidate generation")
    logger.info(
        "Candidate generation finished in %.1fs: candidates=%d, parts=%d, skipped_parts=%d",
        perf_counter() - candidate_started_at,
        candidate_count,
        part_count,
        skipped_count,
    )
    embedded_count = _mark_embedded_after_blocking(
        input_csv,
        lost_rows,
        auction_rows,
    )
    logger.info("Image blocking finished in %.1fs", perf_counter() - run_started_at)
    return BlockingRunResult(
        blocking_root(),
        len(lost_rows),
        len(auction_rows),
        candidate_count,
        part_count,
        skipped_count,
        int(lost_cache.metadata.get("generated_count", 0)),
        embedded_count,
    )


def _prepare_processed_image_replay() -> None:
    with image_storage_lock(env_image_root(), exclusive=False):
        replay_links, replay_artworks = reset_auction_image_matching_for_replay()
    logger.info(
        "Prepared explicit processed-image replay: pending_links=%d pending_artworks=%d",
        replay_links,
        replay_artworks,
    )


def _should_skip_for_empty_db_auction_input(
    input_csv: Path | None,
    include_processed_auction_images: bool,
) -> bool:
    if input_csv is not None or include_processed_auction_images:
        return False
    started_at = perf_counter()
    has_unprocessed = has_unprocessed_auction_image_file_rows()
    logger.info(
        "Checked for unprocessed auction image DB rows in %.1fs: found=%s",
        perf_counter() - started_at,
        has_unprocessed,
    )
    return not has_unprocessed


def _load_inputs(
    input_csv: Path | None,
    lost_limit: int | None,
    auction_limit: int | None,
    include_processed_auction_images: bool,
) -> tuple[list[ImageFileRow], list[ImageFileRow]]:
    if input_csv is not None:
        logger.info("Loading blocking inputs from CSV: %s", input_csv)
        return read_image_file_csv(input_csv)
    logger.info("Loading blocking inputs from Postgres")
    return load_db_image_file_rows(
        lost_limit=lost_limit,
        auction_limit=auction_limit,
        include_processed_auction_images=include_processed_auction_images,
    )


def _effective_auction_limit(
    *,
    input_csv: Path | None,
    auction_limit: int | None,
    include_processed_auction_images: bool,
) -> int | None:
    if input_csv is not None or include_processed_auction_images:
        return auction_limit
    return auction_limit if auction_limit is not None else matching_batch_size_from_env()


def _mark_embedded_after_blocking(
    input_csv: Path | None,
    lost_rows: list[ImageFileRow],
    auction_rows: list[ImageFileRow],
) -> int:
    if input_csv is not None:
        logger.info("Skipping image_file.is_embedded DB update for CSV-backed blocking")
        return 0
    expected: list[ExpectedImageVersion] = []
    for row in [*lost_rows, *auction_rows]:
        if row.content_version is None:
            raise ValueError(
                "DB-backed blocking row is missing content_version: "
                f"image_file_id={row.file_id}"
            )
        expected.append(ExpectedImageVersion(row.file_id, row.content_version))
    return mark_image_files_embedded(expected)


def _candidate_model_identity(metadata: dict[str, object]) -> str:
    value = metadata.get("model_id")
    identity = str(value).strip() if value is not None else ""
    if not identity:
        raise ValueError("Lost embedding cache is missing model_id metadata")
    return identity


def _candidate_lost_source_identity(metadata: dict[str, object]) -> str:
    value = metadata.get("source_identity_sha256")
    identity = str(value).strip() if value is not None else ""
    if not identity:
        raise ValueError(
            "Lost embedding cache is missing source_identity_sha256 metadata"
        )
    return identity


def _candidate_lost_content_sha256(
    metadata: dict[str, object],
    lost_file_ids: list[str],
) -> dict[str, str]:
    raw_identities = metadata.get("source_identities")
    if not isinstance(raw_identities, dict):
        raise ValueError("Lost embedding cache is missing source_identities metadata")
    result: dict[str, str] = {}
    for file_id in lost_file_ids:
        raw_identity = raw_identities.get(file_id)
        if not isinstance(raw_identity, dict):
            raise ValueError(
                "Lost embedding cache is missing a source identity for "
                f"file_id={file_id}"
            )
        value = raw_identity.get("source_sha256")
        digest = str(value).strip().lower() if value is not None else ""
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(
                "Lost embedding cache has an invalid source_sha256 for "
                f"file_id={file_id}"
            )
        result[file_id] = digest
    return result


def _write_candidates(
    auction_rows,
    lost_file_ids,
    lost_embeddings,
    model,
    top_k,
    image_batch_size,
    shard_size,
    *,
    model_identity: str,
    lost_source_identity: str,
    lost_content_versions: dict[str, int | None],
    lost_content_sha256: dict[str, str],
) -> tuple[int, int, int]:
    return write_candidate_parts(
        auction_rows,
        lost_file_ids,
        lost_embeddings,
        model.get,
        model_identity=model_identity,
        lost_source_identity=lost_source_identity,
        lost_content_versions=lost_content_versions,
        lost_content_sha256=lost_content_sha256,
        top_k=top_k,
        image_batch_size=image_batch_size,
        shard_size=shard_size,
    )


class _ModelProvider:
    def __init__(self, *, no_compile: bool, hf_token: str | None) -> None:
        self.token = env_hf_token(hf_token)
        self.no_compile = no_compile
        self._model = None

    def get(self):
        if self._model is None:
            adapter_cls = load_dino_adapter_class()
            started_at = perf_counter()
            logger.info("Initializing DINO adapter (compile=%s)", not self.no_compile)
            self._model = adapter_cls(use_compile=not self.no_compile, hf_token=self.token)
            dimension = int(self._model.get_dimension())
            if dimension <= 0:
                raise RuntimeError("DINOv3 adapter returned an invalid dimension")
            logger.info(
                "DINO adapter ready in %.1fs: model=%s, dim=%d",
                perf_counter() - started_at,
                self._model.get_model_name(),
                dimension,
            )
        return self._model


def _log_sample_rows(role: str, rows: list[ImageFileRow], limit: int = 3) -> None:
    for row in rows[:limit]:
        logger.debug("%s input sample: file_id=%s path=%s", role, row.file_id, row.file_path)
    if len(rows) > limit:
        logger.debug("%s input sample: ... %d more rows", role, len(rows) - limit)


def _prepare_cache_dirs(clear_candidates: bool) -> None:
    if clear_candidates and candidate_dir().exists():
        logger.info("Clearing existing candidate directory: %s", candidate_dir())
        shutil.rmtree(candidate_dir())
    for role in (LOST_ROLE, AUCTION_ROLE):
        (blocking_root() / role).mkdir(parents=True, exist_ok=True)
    candidate_dir().mkdir(parents=True, exist_ok=True)


def _validate_args(
    top_k: int,
    image_batch_size: int,
    candidate_shard_size: int,
    lost_limit: int | None,
    auction_limit: int | None,
) -> None:
    for name, value in {
        "top_k": top_k,
        "image_batch_size": image_batch_size,
        "candidate_shard_auction_images": candidate_shard_size,
    }.items():
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    for name, value in {
        "lost_limit": lost_limit,
        "auction_limit": auction_limit,
    }.items():
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be positive")
    if top_k > 32_767:
        raise ValueError("top_k must fit into the int16 candidate rank column")
