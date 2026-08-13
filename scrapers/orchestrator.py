"""Scraper orchestrator for smARTmatch.

Provides both a programmatic API and a CLI for running scrapers,
tracking run history, and querying stats.

CLI usage:
    python -m scrapers.orchestrator run christies
    python -m scrapers.orchestrator run-all
    python -m scrapers.orchestrator status
    python -m scrapers.orchestrator history christies

Programmatic usage:
    from scrapers.orchestrator import Orchestrator

    orch = Orchestrator()
    orch.run_scraper("christies")
    print(orch.get_all_status())
"""

from __future__ import annotations

import argparse
import inspect
import math
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import Column, DateTime, Integer, String, Text, func, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from shared.logging_adapter import configure_logging, get_logger
from scrapers.db_interface import AuctionArtwork, AuctionPlatform, Database, LostArtwork
from scrapers.run_lock import try_acquire_scraper_lock
from scrapers.runtime_config import load_request_cooldown_override
from scrapers.utils.image_storage import platform_image_prefix


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# ScraperRun ORM model (separate declarative base to avoid touching models.py)
# ---------------------------------------------------------------------------


class _OrchestratorBase(DeclarativeBase):
    pass


class ScraperRun(_OrchestratorBase):
    __tablename__ = "scraper_run"

    run_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    scraper_name = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False, default="running")
    started_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime(timezone=True))
    entries_scraped = Column(Integer, nullable=False, default=0)
    entries_skipped = Column(Integer, nullable=False, default=0)
    total_entries = Column(Integer, nullable=False, default=0)
    queue_total = Column(Integer, nullable=False, default=0)
    queue_processed = Column(Integer, nullable=False, default=0)
    progress_updated_at = Column(DateTime(timezone=True))
    error_message = Column(Text)


# ---------------------------------------------------------------------------
# Scraper registry
# ---------------------------------------------------------------------------

SCRAPER_REGISTRY: dict[str, dict[str, Any]] = {
    "christies": {
        "display_name": "Christie's",
        "module": "scrapers.christies.scraper",
        "class_name": "ChristiesScraper",
        "platform_name": "Christie's",
        "table": "auction",
    },
    "sothebys": {
        "display_name": "Sotheby's",
        "module": "scrapers.sothebys.scraper",
        "class_name": "SothebysScraper",
        "platform_name": "sothebys",
        "table": "auction",
    },
    "drouot": {
        "display_name": "Drouot",
        "module": "scrapers.drouot.scraper",
        "class_name": "DrouotScraper",
        "platform_name": "Drouot",
        "table": "auction",
    },
    "lottissimo": {
        "display_name": "Lot-Tissimo",
        "module": "scrapers.lottissimo.scraper",
        "class_name": "LottissimoScraper",
        "platform_name": "lot-tissimo",
        "table": "auction",
    },
    "dorotheum": {
        "display_name": "Dorotheum",
        "module": "scrapers.dorotheum.scraper",
        "class_name": "DorotheumScraper",
        "platform_name": "Dorotheum",
        "table": "auction",
    },
    "lostart": {
        "display_name": "Lost Art",
        "module": "scrapers.lostart.scraper",
        "class_name": "LostArtScraper",
        "platform_name": None,
        "table": "lost",
    },
}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """Main orchestrator for running and monitoring scrapers."""

    DEFAULT_REQUEST_COOLDOWN_SECONDS: float = 5.0
    PROGRESS_FLUSH_INTERVAL_SECONDS: float = 2.0
    REQUEST_COOLDOWN_ENV_VARS: tuple[str, ...] = (
        "SCRAPER_REQUEST_COOLDOWN_SECONDS",
        "SMARTMATCH_SCRAPER_REQUEST_COOLDOWN_SECONDS",
        "SCRAPER_DASHBOARD_REQUEST_COOLDOWN_SECONDS",
    )

    def __init__(
        self,
        request_cooldown_seconds: Optional[float] = None,
        *,
        reconcile_interrupted_runs: bool = True,
    ):
        if request_cooldown_seconds is None:
            request_cooldown_seconds = self._request_cooldown_from_env()
        self.request_cooldown_seconds: float = self._validate_cooldown(
            request_cooldown_seconds,
            source="request_cooldown_seconds",
        )
        self._ensure_table()
        self._running: dict[str, threading.Thread] = {}
        self._active_runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._reconcile_lock = threading.Lock()
        if reconcile_interrupted_runs:
            self._finalize_interrupted_runs()
        self._last_reconcile_monotonic = time.monotonic()

    # -- Configuration helpers ---------------------------------------------

    @classmethod
    def _validate_cooldown(cls, seconds: Any, *, source: str) -> float:
        """Return a validated non-negative cooldown value in seconds."""
        try:
            cooldown = float(seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{source} must be a non-negative number of seconds, got {seconds!r}"
            ) from exc

        if cooldown < 0 or not math.isfinite(cooldown):
            raise ValueError(f"{source} must be a finite non-negative number, got {cooldown}")

        return cooldown

    @classmethod
    def _request_cooldown_from_env(cls) -> float:
        """Return the shared runtime override, environment value, or default."""
        runtime_override = load_request_cooldown_override()
        if runtime_override is not None:
            return cls._validate_cooldown(
                runtime_override,
                source="scraper runtime config",
            )

        for name in cls.REQUEST_COOLDOWN_ENV_VARS:
            value = (os.getenv(name) or "").strip()
            if value:
                return cls._validate_cooldown(value, source=name)

        return cls.DEFAULT_REQUEST_COOLDOWN_SECONDS

    # -- DB helpers ---------------------------------------------------------

    def _engine(self):
        db = Database()
        return db.engine

    def _session(self) -> Session:
        engine = self._engine()
        return sessionmaker(bind=engine)()

    def _ensure_table(self) -> None:
        """Create the scraper_run table if it doesn't exist."""
        engine = self._engine()
        _OrchestratorBase.metadata.create_all(engine, tables=[ScraperRun.__table__], checkfirst=True)

    def _finalize_interrupted_runs(self) -> None:
        """Mark rows stale only when no process owns that scraper's DB lock."""
        for scraper_name in SCRAPER_REGISTRY:
            if not self._has_unfinished_run(scraper_name):
                continue
            lease = try_acquire_scraper_lock(self._engine(), scraper_name)
            if lease is None:
                continue
            try:
                self._finalize_interrupted_runs_for_scraper(scraper_name)
            finally:
                lease.release()

    def _has_unfinished_run(self, scraper_name: str) -> bool:
        session = self._session()
        try:
            row = session.execute(
                select(ScraperRun.run_id)
                .where(
                    ScraperRun.scraper_name == scraper_name,
                    ScraperRun.status == "running",
                    ScraperRun.finished_at.is_(None),
                )
                .limit(1)
            ).first()
            return row is not None
        finally:
            session.close()

    def _finalize_interrupted_runs_for_scraper(self, scraper_name: str) -> None:
        """Finalize stale rows while the caller exclusively owns the scraper lock."""
        session = self._session()
        try:
            stale_runs = session.execute(
                select(ScraperRun).where(
                    ScraperRun.scraper_name == scraper_name,
                    ScraperRun.status == "running",
                    ScraperRun.finished_at.is_(None),
                )
            ).scalars().all()
            if not stale_runs:
                return

            now = datetime.now(timezone.utc)
            for run in stale_runs:
                self._mark_run_interrupted(run, finished_at=now)
                # entries_skipped is left at its last batch-flush value because
                # the exited scraper instance cannot be inspected.
                self._backfill_counts(session, run)
            session.commit()
        finally:
            session.close()

    def _maybe_finalize_interrupted_runs(self, interval_seconds: float = 30.0) -> None:
        """Periodically reconcile crashed child workers for dashboard polling."""
        now = time.monotonic()
        if now - self._last_reconcile_monotonic < interval_seconds:
            return
        if not self._reconcile_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            if now - self._last_reconcile_monotonic < interval_seconds:
                return
            self._finalize_interrupted_runs()
            self._last_reconcile_monotonic = now
        finally:
            self._reconcile_lock.release()

    _INTERRUPTED_MSG = (
        "Interrupted: scraper worker exited before recording completion."
    )

    @classmethod
    def _mark_run_interrupted(cls, run: ScraperRun, *, finished_at: datetime) -> None:
        """Stamp a stale 'running' row as failed with the interrupted message."""
        run.status = "failed"
        run.finished_at = finished_at
        run.error_message = (
            f"{run.error_message}\n{cls._INTERRUPTED_MSG}"
            if run.error_message
            else cls._INTERRUPTED_MSG
        )

    @classmethod
    def _snapshot_queue_progress(
        cls,
        run: ScraperRun,
        scraper_instance: Any,
        *,
        updated_at: Optional[datetime] = None,
    ) -> None:
        """Copy process-local queue counters onto a shared run row."""
        stats = getattr(scraper_instance, "stats", None)
        if not isinstance(stats, dict):
            return

        queue_total = cls._to_non_negative_int(stats.get("urls_total"))
        queue_processed = cls._to_non_negative_int(stats.get("urls_processed"))
        run.queue_processed = queue_processed
        run.queue_total = max(queue_total, queue_processed)
        run.progress_updated_at = updated_at or datetime.now(timezone.utc)

    def _backfill_counts(
        self,
        session: Session,
        run: ScraperRun,
        *,
        scraper_instance: Any = None,
    ) -> None:
        """Populate run counts from DB state (and live scraper stats if given).

        Used by both the per-batch progress flush and the post-restart
        interrupted-run finalizer so they stay in sync. If ``scraper_instance``
        is None, ``entries_skipped`` is left untouched (the instance is gone,
        so the in-memory skip counters cannot be recovered).
        """
        if run.scraper_name not in SCRAPER_REGISTRY:
            return
        if scraper_instance is not None:
            self._snapshot_queue_progress(run, scraper_instance)
        try:
            count_after = self._count_entries(session, run.scraper_name)
        except Exception:
            return
        baseline = self._baseline_from_prior_run(
            session, run.scraper_name, run.run_id
        )
        run.total_entries = count_after
        run.entries_scraped = max(0, count_after - baseline)
        if scraper_instance is None:
            return
        run.entries_skipped = self._calculate_entries_skipped(
            urls_processed=run.queue_processed,
            entries_scraped=run.entries_scraped,
            scraper_instance=scraper_instance,
        )

    def _flush_progress(
        self,
        run_id: Any,
        scraper_instance: Any,
        *,
        include_entry_counts: bool = True,
    ) -> None:
        """Snapshot live counters onto the run row in its own session.

        Frequent queue updates avoid expensive table counts; batch checkpoints
        additionally refresh New/Skipped/Total. A missed checkpoint must never
        break the scrape loop, but it is logged so frozen progress is diagnosable.
        """
        session = self._session()
        try:
            run = session.get(ScraperRun, run_id)
            if run is None or run.status != "running":
                return
            if include_entry_counts:
                self._backfill_counts(session, run, scraper_instance=scraper_instance)
            else:
                self._snapshot_queue_progress(run, scraper_instance)
            session.commit()
        except Exception:
            session.rollback()
            logger.warning(
                "scraper=%s run_id=%s progress checkpoint failed",
                getattr(scraper_instance, "platform_name", "unknown"),
                run_id,
                exc_info=True,
            )
        finally:
            session.close()

    def _baseline_from_prior_run(
        self,
        session: Session,
        scraper_name: str,
        current_run_id: Any,
    ) -> int:
        """Return previous finished run's total_entries as a baseline.

        Used to derive "new entries" purely from DB state, so the metric is
        consistent across completed, failed, and interrupted runs.
        """
        prior_run = session.execute(
            select(ScraperRun)
            .where(
                ScraperRun.scraper_name == scraper_name,
                ScraperRun.run_id != current_run_id,
                ScraperRun.finished_at.is_not(None),
            )
            .order_by(ScraperRun.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return int(prior_run.total_entries or 0) if prior_run else 0

    def _count_entries(self, session: Session, scraper_name: str) -> int:
        """Count existing entries in DB for a specific scraper."""
        info = SCRAPER_REGISTRY[scraper_name]

        if info["table"] == "lost":
            return session.scalar(select(func.count()).select_from(LostArtwork)) or 0

        platform = session.execute(
            select(AuctionPlatform).where(AuctionPlatform.name == info["platform_name"])
        ).scalar_one_or_none()

        if platform is None:
            return 0

        return session.scalar(
            select(func.count())
            .select_from(AuctionArtwork)
            .where(AuctionArtwork.auction_platform_id == platform.auction_platform_id)
        ) or 0

    @staticmethod
    def _to_non_negative_int(value: Any) -> int:
        """Coerce arbitrary values to non-negative ints for metric math."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, parsed)

    @classmethod
    def _prefiltered_skipped_count(cls, scraper_instance: Any) -> int:
        """Return lots skipped before processing (e.g. already in DB)."""
        if scraper_instance is None:
            return 0

        counts: list[int] = []

        stats = getattr(scraper_instance, "stats", None)
        if isinstance(stats, dict):
            for key in (
                "urls_skipped_prefiltered",
                "urls_skipped_existing",
                "entries_skipped_prefiltered",
            ):
                counts.append(cls._to_non_negative_int(stats.get(key)))

        for attr in ("_skipped_existing", "_prefiltered_skipped", "prefiltered_skipped"):
            counts.append(cls._to_non_negative_int(getattr(scraper_instance, attr, 0)))

        return max(counts) if counts else 0

    @classmethod
    def _calculate_entries_skipped(
        cls,
        *,
        urls_processed: Any,
        entries_scraped: Any,
        scraper_instance: Any = None,
    ) -> int:
        """Combine in-loop skips with prefilter skips into one dashboard metric."""
        processed_count = cls._to_non_negative_int(urls_processed)
        scraped_count = cls._to_non_negative_int(entries_scraped)
        runtime_skipped = max(0, processed_count - scraped_count)
        return runtime_skipped + cls._prefiltered_skipped_count(scraper_instance)

    @staticmethod
    def _first_env_value(*names: str) -> str:
        """Return first non-empty environment variable value from names."""
        for name in names:
            value = (os.getenv(name) or "").strip()
            if value:
                return value
        return ""

    @classmethod
    def _merge_env_scraper_kwargs(
        cls,
        *,
        scraper_name: str,
        scraper_kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Inject scraper-specific cookie headers from env when not passed explicitly."""
        if scraper_name != "dorotheum":
            return dict(scraper_kwargs)

        merged = dict(scraper_kwargs)

        if not merged.get("cookie_header"):
            cookie_header = cls._first_env_value(
                "SMARTMATCH_DOROTHEUM_COOKIE_HEADER",
                "DOROTHEUM_COOKIE_HEADER",
            )
            if cookie_header:
                merged["cookie_header"] = cookie_header

        return merged

    # -- Cooldown injection -------------------------------------------------

    # Known delay parameter names used across auction scrapers.
    _DELAY_PARAM_NAMES: frozenset[str] = frozenset(("min_wait", "max_wait"))

    def _inject_cooldown_kwarg(self, scraper_class: type, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Inject cooldown into recognised delay params accepted by the scraper.

        Only sets params that are (a) in the scraper's __init__ signature and
        (b) not already provided by the caller.
        """
        try:
            accepted = inspect.signature(scraper_class.__init__).parameters.keys()
        except (ValueError, TypeError):
            return kwargs

        to_inject = {
            param: self.request_cooldown_seconds
            for param in self._DELAY_PARAM_NAMES
            if param in accepted and param not in kwargs
        }
        if not to_inject:
            return kwargs
        return {**kwargs, **to_inject}

    # -- Import scraper class -----------------------------------------------

    @staticmethod
    def _import_scraper_class(scraper_name: str):
        """Dynamically import and return the scraper class."""
        import importlib

        info = SCRAPER_REGISTRY[scraper_name]
        module = importlib.import_module(info["module"])
        return getattr(module, info["class_name"])

    # -- Run a single scraper -----------------------------------------------

    def run_scraper(
        self,
        scraper_name: str,
        *,
        background: bool = False,
        **scraper_kwargs,
    ) -> dict:
        """Run a scraper and record the run.

        Args:
            scraper_name: Key in SCRAPER_REGISTRY.
            background: If True, run in a background thread.
            **scraper_kwargs: Passed to the scraper constructor (e.g. max_pages=5).
                Delay params (min_wait, max_wait) default to the orchestrator's
                request_cooldown_seconds if not set explicitly.

        Returns:
            Dict with run_id and status info.
        """
        if scraper_name not in SCRAPER_REGISTRY:
            raise ValueError(f"Unknown scraper: {scraper_name}. Available: {list(SCRAPER_REGISTRY)}")

        if background:
            with self._lock:
                running_thread = self._running.get(scraper_name)
                if running_thread is not None and running_thread.is_alive():
                    return {"error": f"Scraper '{scraper_name}' is already running."}

                run_id = str(uuid4())
                thread = threading.Thread(
                    target=self._run_scraper_guarded,
                    args=(scraper_name,),
                    kwargs={**scraper_kwargs, "_run_id": run_id},
                    daemon=True,
                )
                self._running[scraper_name] = thread
                thread.start()
            return {"run_id": run_id, "status": "started", "scraper": scraper_name}

        return self._run_scraper_guarded(scraper_name, **scraper_kwargs)

    def _run_scraper_guarded(
        self,
        scraper_name: str,
        _run_id: Optional[str] = None,
        **scraper_kwargs,
    ) -> dict:
        """Acquire the cross-process lease before creating or changing run rows."""
        lease = None
        for attempt in range(20):
            lease = try_acquire_scraper_lock(self._engine(), scraper_name)
            if lease is not None:
                break
            if attempt < 19:
                # Reconciliation briefly uses the same lock. Retrying prevents
                # that maintenance window from being mistaken for a live run.
                time.sleep(0.1)
        if lease is None:
            with self._lock:
                self._running.pop(scraper_name, None)
            return {
                "scraper": scraper_name,
                "status": "skipped",
                "reason": "already_running",
            }

        try:
            self._finalize_interrupted_runs_for_scraper(scraper_name)
            return self._run_scraper_sync(
                scraper_name,
                _run_id=_run_id,
                _lease=lease,
                **scraper_kwargs,
            )
        finally:
            lease.release()

    def _run_scraper_sync(
        self,
        scraper_name: str,
        _run_id: Optional[str] = None,
        _lease: Any = None,
        **scraper_kwargs,
    ) -> dict:
        """Synchronous scraper execution with run tracking."""
        session = self._session()
        run_id = _run_id or str(uuid4())

        # Record the start of the run.
        run = ScraperRun(
            run_id=run_id,
            scraper_name=scraper_name,
            status="running",
        )
        session.add(run)
        session.commit()

        count_before = 0
        scraper_instance = None

        def _collect_entry_counts() -> tuple[int, int]:
            """Return (count_after, new_entries) derived from DB state.

            Uses the previous finished run's total_entries as the baseline so
            the metric is consistent across completed/failed/interrupted runs.
            """
            session.expire_all()
            count_after = self._count_entries(session, scraper_name)
            baseline = self._baseline_from_prior_run(session, scraper_name, run_id)
            return count_after, max(0, count_after - baseline)

        def _urls_processed_value(instance: Any) -> Any:
            if instance is None:
                return 0
            stats = getattr(instance, "stats", None)
            if not isinstance(stats, dict):
                return 0
            return stats.get("urls_processed", 0)

        try:
            # Count entries before.
            count_before = self._count_entries(session, scraper_name)

            # Import and instantiate the scraper.
            ScraperClass = self._import_scraper_class(scraper_name)
            resolved_scraper_kwargs = self._merge_env_scraper_kwargs(
                scraper_name=scraper_name,
                scraper_kwargs=scraper_kwargs,
            )
            resolved_scraper_kwargs = self._inject_cooldown_kwarg(ScraperClass, resolved_scraper_kwargs)
            scraper_instance = ScraperClass(**resolved_scraper_kwargs)

            with self._lock:
                self._active_runs[scraper_name] = {
                    "run_id": run_id,
                    "count_before": count_before,
                    "scraper_instance": scraper_instance,
                    "started_at": datetime.now(timezone.utc),
                }

            # Publish queue counters independently of process memory so the
            # dashboard can observe child workers. Batch commits also refresh
            # DB-derived New/Skipped/Total values.
            last_progress_flush = 0.0

            def publish_progress() -> None:
                nonlocal last_progress_flush
                now = time.monotonic()
                if now - last_progress_flush < self.PROGRESS_FLUSH_INTERVAL_SECONDS:
                    return
                if _lease is not None:
                    _lease.ensure_held()
                self._flush_progress(
                    run_id,
                    scraper_instance,
                    include_entry_counts=False,
                )
                last_progress_flush = now

            def checkpoint_progress() -> None:
                nonlocal last_progress_flush
                if _lease is not None:
                    _lease.ensure_held()
                self._flush_progress(run_id, scraper_instance)
                last_progress_flush = time.monotonic()

            if _lease is not None:
                scraper_instance.on_before_batch_commit = _lease.ensure_held
            scraper_instance.on_progress = publish_progress
            scraper_instance.on_batch_commit = checkpoint_progress

            # A lost lock fences this worker at startup, each batch commit, and
            # completion instead of allowing a second process to overlap it.
            if _lease is not None:
                _lease.ensure_held()
            scraper_instance.run()
            if _lease is not None:
                _lease.ensure_held()

            # Count entries after.
            count_after, new_entries = _collect_entry_counts()

            urls_processed = _urls_processed_value(scraper_instance)
            skipped = self._calculate_entries_skipped(
                urls_processed=urls_processed,
                entries_scraped=new_entries,
                scraper_instance=scraper_instance,
            )

            # Update run record.
            run.status = "completed"
            run.finished_at = datetime.now(timezone.utc)
            run.entries_scraped = new_entries
            run.entries_skipped = skipped
            run.total_entries = count_after
            self._snapshot_queue_progress(run, scraper_instance)
            session.commit()

            result = {
                "run_id": run_id,
                "scraper": scraper_name,
                "status": "completed",
                "entries_scraped": new_entries,
                "entries_skipped": skipped,
                "total_entries": count_after,
            }

        except Exception as exc:
            error_traceback = traceback.format_exc()
            # A failed query or commit leaves SQLAlchemy's transaction unusable
            # until rollback. The run's initial row was committed separately.
            session.rollback()
            try:
                count_after, new_entries = _collect_entry_counts()
            except Exception:
                session.rollback()
                count_after = max(0, int(count_before or 0))
                new_entries = 0

            urls_processed = _urls_processed_value(scraper_instance)
            skipped = self._calculate_entries_skipped(
                urls_processed=urls_processed,
                entries_scraped=new_entries,
                scraper_instance=scraper_instance,
            )

            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.entries_scraped = new_entries
            run.entries_skipped = skipped
            run.total_entries = count_after
            if scraper_instance is not None:
                self._snapshot_queue_progress(run, scraper_instance)
            run.error_message = error_traceback
            session.commit()
            result = {
                "run_id": run_id,
                "scraper": scraper_name,
                "status": "failed",
                "entries_scraped": new_entries,
                "entries_skipped": skipped,
                "total_entries": count_after,
                "error": str(exc),
            }

        finally:
            with self._lock:
                self._running.pop(scraper_name, None)
                self._active_runs.pop(scraper_name, None)
            session.close()

        return result

    # -- Run all scrapers ---------------------------------------------------

    def run_all(self, *, background: bool = False, **scraper_kwargs) -> list[dict]:
        """Run all registered scrapers sequentially.

        If background=True, the entire sequence runs in a single background thread.
        """
        if background:
            t = threading.Thread(
                target=self._run_all_sync,
                kwargs=scraper_kwargs,
                daemon=True,
            )
            t.start()
            return [{"status": "started", "scrapers": list(SCRAPER_REGISTRY)}]

        return self._run_all_sync(**scraper_kwargs)

    def _run_all_sync(self, **scraper_kwargs) -> list[dict]:
        results = []
        for name in SCRAPER_REGISTRY:
            result = self.run_scraper(name, **scraper_kwargs)
            results.append(result)
        return results

    # -- Status & stats -----------------------------------------------------

    def is_running(self, scraper_name: str) -> bool:
        with self._lock:
            t = self._running.get(scraper_name)
            return t is not None and t.is_alive()

    @classmethod
    def _persisted_run_progress(
        cls,
        run: ScraperRun,
        *,
        total_entries: int,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Build dashboard progress from the process-shared run record."""
        queue_total = cls._to_non_negative_int(getattr(run, "queue_total", 0))
        queue_processed = cls._to_non_negative_int(
            getattr(run, "queue_processed", 0)
        )
        queue_total = max(queue_total, queue_processed)

        started_at = run.started_at
        if started_at and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        current_time = now or datetime.now(timezone.utc)
        elapsed_seconds = 0
        if started_at:
            elapsed_seconds = max(
                0,
                int((current_time - started_at).total_seconds()),
            )

        progress_percent = None
        if queue_total > 0:
            progress_percent = round(
                min(100.0, (queue_processed / queue_total) * 100.0),
                1,
            )

        eta_seconds = None
        if elapsed_seconds > 0 and 0 < queue_processed < queue_total:
            rate = queue_processed / elapsed_seconds
            if rate > 0:
                eta_seconds = int((queue_total - queue_processed) / rate)

        return {
            "run_id": str(run.run_id),
            "urls_total": queue_total,
            "urls_processed": queue_processed,
            "entries_scraped_estimate": cls._to_non_negative_int(
                run.entries_scraped
            ),
            "entries_skipped_estimate": cls._to_non_negative_int(
                run.entries_skipped
            ),
            "total_entries_estimate": max(
                cls._to_non_negative_int(total_entries),
                cls._to_non_negative_int(run.total_entries),
            ),
            "progress_percent": progress_percent,
            "elapsed_seconds": elapsed_seconds,
            "eta_seconds": eta_seconds,
            "progress_updated_at": (
                run.progress_updated_at.isoformat()
                if getattr(run, "progress_updated_at", None)
                else None
            ),
        }

    def get_all_status(self) -> list[dict]:
        """Return status overview for all scrapers."""
        self._maybe_finalize_interrupted_runs()
        session = self._session()
        try:
            results = []
            for name, info in SCRAPER_REGISTRY.items():
                # Last completed run.
                last_run = session.execute(
                    select(ScraperRun)
                    .where(ScraperRun.scraper_name == name)
                    .order_by(ScraperRun.started_at.desc())
                    .limit(1)
                ).scalar_one_or_none()

                total = self._count_entries(session, name)
                progress = None
                with self._lock:
                    active = self._active_runs.get(name)

                if active:
                    scraper_instance = active.get("scraper_instance")
                    stats = getattr(scraper_instance, "stats", {}) if scraper_instance else {}
                    urls_total = int(stats.get("urls_total", 0) or 0)
                    urls_processed = int(stats.get("urls_processed", 0) or 0)
                    count_before = int(active.get("count_before", 0) or 0)
                    started_at = active.get("started_at")

                    # Some scrapers expose a more direct processed-new counter.
                    processed_attr = getattr(scraper_instance, "_processed", None) if scraper_instance else None
                    if isinstance(processed_attr, int) and processed_attr >= 0:
                        entries_scraped_est = processed_attr
                    else:
                        entries_scraped_est = max(0, total - count_before)

                    entries_skipped_est = self._calculate_entries_skipped(
                        urls_processed=urls_processed,
                        entries_scraped=entries_scraped_est,
                        scraper_instance=scraper_instance,
                    )

                    # Keep queue display sane when scraper stats are partial/inconsistent.
                    if urls_total > 0 and urls_processed > urls_total:
                        urls_total = urls_processed

                    progress_pct = None
                    if urls_total > 0:
                        progress_pct = min(100.0, (urls_processed / urls_total) * 100.0)

                    elapsed_seconds = 0
                    if isinstance(started_at, datetime):
                        elapsed_seconds = max(0, int((datetime.now(timezone.utc) - started_at).total_seconds()))

                    eta_seconds = None
                    if elapsed_seconds > 0 and urls_processed > 0 and urls_total > urls_processed:
                        rate = urls_processed / elapsed_seconds
                        if rate > 0:
                            eta_seconds = int((urls_total - urls_processed) / rate)

                    progress = {
                        "run_id": active.get("run_id"),
                        "urls_total": urls_total,
                        "urls_processed": urls_processed,
                        "entries_scraped_estimate": entries_scraped_est,
                        "entries_skipped_estimate": entries_skipped_est,
                        "total_entries_estimate": max(total, count_before + entries_scraped_est),
                        "progress_percent": round(progress_pct, 1) if progress_pct is not None else None,
                        "elapsed_seconds": elapsed_seconds,
                        "eta_seconds": eta_seconds,
                    }

                persisted_running = bool(last_run and last_run.status == "running")
                if progress is None and persisted_running:
                    progress = self._persisted_run_progress(
                        last_run,
                        total_entries=total,
                    )

                results.append({
                    "name": name,
                    "display_name": info["display_name"],
                    "is_running": self.is_running(name) or persisted_running,
                    "total_entries": total,
                    "progress": progress,
                    "last_run": _run_to_dict(last_run) if last_run else None,
                })
            return results
        finally:
            session.close()

    def get_run_history(self, scraper_name: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Return run history, optionally filtered by scraper."""
        session = self._session()
        try:
            stmt = select(ScraperRun).order_by(ScraperRun.started_at.desc()).limit(limit)
            if scraper_name:
                stmt = stmt.where(ScraperRun.scraper_name == scraper_name)
            rows = session.execute(stmt).scalars().all()

            out: list[dict] = []
            for row in rows:
                run_dict = _run_to_dict(row)

                # Overlay live estimates for currently running scraper rows.
                if row.status == "running":
                    with self._lock:
                        active = self._active_runs.get(row.scraper_name)

                    if active and str(active.get("run_id")) == str(row.run_id):
                        scraper_instance = active.get("scraper_instance")
                        stats = getattr(scraper_instance, "stats", {}) if scraper_instance else {}
                        urls_total = int(stats.get("urls_total", 0) or 0)
                        urls_processed = int(stats.get("urls_processed", 0) or 0)
                        count_before = int(active.get("count_before", 0) or 0)

                        processed_attr = getattr(scraper_instance, "_processed", None) if scraper_instance else None
                        if isinstance(processed_attr, int) and processed_attr >= 0:
                            entries_scraped_est = processed_attr
                        else:
                            entries_scraped_est = max(0, self._count_entries(session, row.scraper_name) - count_before)

                        entries_skipped_est = self._calculate_entries_skipped(
                            urls_processed=urls_processed,
                            entries_scraped=entries_scraped_est,
                            scraper_instance=scraper_instance,
                        )
                        if urls_total > 0 and urls_processed > urls_total:
                            urls_total = urls_processed

                        run_dict["entries_scraped"] = entries_scraped_est
                        run_dict["entries_skipped"] = entries_skipped_est
                        run_dict["total_entries"] = max(
                            int(run_dict.get("total_entries") or 0),
                            count_before + entries_scraped_est,
                        )
                        run_dict["queue_processed"] = urls_processed
                        run_dict["queue_total"] = urls_total

                out.append(run_dict)

            return out
        finally:
            session.close()

    # -- Cooldown configuration --------------------------------------------

    def get_cooldown(self) -> float:
        """Return the current per-request cooldown in seconds."""
        return self.request_cooldown_seconds

    def set_cooldown(self, seconds: float) -> None:
        """Set the per-request cooldown in seconds (applied to future runs)."""
        self.request_cooldown_seconds = self._validate_cooldown(
            seconds,
            source="request_cooldown_seconds",
        )

    def get_scraper_names(self) -> list[str]:
        return list(SCRAPER_REGISTRY.keys())

    def get_scraper_info(self, name: str) -> dict:
        info = SCRAPER_REGISTRY[name]
        return {
            "name": name,
            "display_name": info["display_name"],
            "table": info["table"],
        }


def _run_to_dict(run: ScraperRun) -> dict:
    return {
        "run_id": str(run.run_id),
        "scraper_name": run.scraper_name,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "entries_scraped": run.entries_scraped,
        "entries_skipped": run.entries_skipped,
        "total_entries": run.total_entries,
        "queue_processed": run.queue_processed,
        "queue_total": run.queue_total,
        "progress_updated_at": (
            run.progress_updated_at.isoformat() if run.progress_updated_at else None
        ),
        "error_message": run.error_message,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cli() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="smARTmatch scraper orchestrator.",
    )
    sub = parser.add_subparsers(dest="command")

    # -- run <scraper>
    run_p = sub.add_parser("run", help="Run a single scraper.")
    run_p.add_argument("scraper", choices=list(SCRAPER_REGISTRY))
    run_p.add_argument("--max-pages", type=int, default=None)
    run_p.add_argument("--skip", type=int, default=0)
    run_p.add_argument("--skip-images", action="store_true")
    run_p.add_argument("--purge", action="store_true")
    run_p.add_argument(
        "--request-delay",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Per-request cooldown in seconds "
            f"(default: {Orchestrator.DEFAULT_REQUEST_COOLDOWN_SECONDS}; "
            "env: SCRAPER_REQUEST_COOLDOWN_SECONDS)"
        ),
    )

    # -- run-all
    run_all_p = sub.add_parser("run-all", help="Run all scrapers sequentially.")
    run_all_p.add_argument("--max-pages", type=int, default=None)
    run_all_p.add_argument("--skip-images", action="store_true")
    run_all_p.add_argument(
        "--request-delay",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Per-request cooldown in seconds "
            f"(default: {Orchestrator.DEFAULT_REQUEST_COOLDOWN_SECONDS}; "
            "env: SCRAPER_REQUEST_COOLDOWN_SECONDS)"
        ),
    )

    # -- status
    sub.add_parser("status", help="Show current status of all scrapers.")

    # -- history
    hist_p = sub.add_parser("history", help="Show run history.")
    hist_p.add_argument("scraper", nargs="?", default=None)
    hist_p.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    cooldown = getattr(args, "request_delay", None)
    orch = Orchestrator(request_cooldown_seconds=cooldown)

    if args.command == "run":
        kwargs = {}
        if args.max_pages is not None:
            kwargs["max_pages"] = args.max_pages
        if args.skip:
            kwargs["skip"] = args.skip  # Note: forwarded but scraper.run() uses skip= kwarg
        if args.purge:
            kwargs["purge"] = args.purge
        result = orch.run_scraper(args.scraper, **kwargs)
        _print_result(result)

    elif args.command == "run-all":
        kwargs = {}
        if args.max_pages is not None:
            kwargs["max_pages"] = args.max_pages
        results = orch.run_all(**kwargs)
        for r in results:
            _print_result(r)

    elif args.command == "status":
        statuses = orch.get_all_status()
        _print_status_table(statuses)

    elif args.command == "history":
        runs = orch.get_run_history(scraper_name=args.scraper, limit=args.limit)
        _print_history(runs)


def _print_result(result: dict) -> None:
    status = result.get("status", "unknown")
    scraper = result.get("scraper", "?")
    prefix = platform_image_prefix(scraper) if scraper != "?" else "scr"
    if status == "completed":
        print(
            f"[{prefix}] [done] "
            f"{result.get('entries_scraped', 0)} new, "
            f"{result.get('entries_skipped', 0)} skipped, "
            f"{result.get('total_entries', 0)} total"
        )
    elif status == "failed":
        print(f"[{prefix}] [fail] {result.get('error', 'unknown error')}")
    elif status == "started":
        print(f"[{prefix}] [start] background run")
    else:
        print(f"[{prefix}] [info] {result}")


def _print_status_table(statuses: list[dict]) -> None:
    header = f"{'Scraper':<16} {'Status':<12} {'Total':<8} {'Last Run':<22} {'New':<6} {'Skipped':<8}"
    print(header)
    print("-" * len(header))
    for s in statuses:
        status_str = "RUNNING" if s["is_running"] else "idle"
        lr = s.get("last_run")
        if lr:
            last_str = lr["started_at"][:19].replace("T", " ")
            new_str = str(lr["entries_scraped"])
            skip_str = str(lr["entries_skipped"])
        else:
            last_str = "never"
            new_str = "-"
            skip_str = "-"
        print(
            f"{s['display_name']:<16} {status_str:<12} {s['total_entries']:<8} "
            f"{last_str:<22} {new_str:<6} {skip_str:<8}"
        )


def _print_history(runs: list[dict]) -> None:
    if not runs:
        print("No runs recorded.")
        return
    header = f"{'Scraper':<16} {'Status':<12} {'Started':<22} {'New':<6} {'Skipped':<8} {'Total':<8}"
    print(header)
    print("-" * len(header))
    for r in runs:
        started = (r["started_at"] or "")[:19].replace("T", " ")
        print(
            f"{r['scraper_name']:<16} {r['status']:<12} {started:<22} "
            f"{r['entries_scraped']:<6} {r['entries_skipped']:<8} {r['total_entries']:<8}"
        )


if __name__ == "__main__":
    try:
        cli()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
