from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from scrapers.orchestrator import Orchestrator


class _FakeSession:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0
        self.closed = False
        self.expire_calls = 0
        self.rollbacks = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True

    def rollback(self) -> None:
        self.rollbacks += 1

    def expire_all(self) -> None:
        self.expire_calls += 1


class OrchestratorSkipMetricTests(unittest.TestCase):
    def test_snapshot_queue_progress_persists_worker_stats(self) -> None:
        updated_at = datetime(2026, 8, 12, tzinfo=timezone.utc)
        run = SimpleNamespace(
            queue_total=0,
            queue_processed=0,
            progress_updated_at=None,
        )
        scraper = SimpleNamespace(
            stats={"urls_total": 40, "urls_processed": 12},
        )

        Orchestrator._snapshot_queue_progress(
            run,
            scraper,
            updated_at=updated_at,
        )

        self.assertEqual(run.queue_total, 40)
        self.assertEqual(run.queue_processed, 12)
        self.assertEqual(run.progress_updated_at, updated_at)

    def test_persisted_progress_supports_cross_process_dashboard(self) -> None:
        now = datetime(2026, 8, 12, 12, 5, tzinfo=timezone.utc)
        run = SimpleNamespace(
            run_id="run-1",
            started_at=now - timedelta(minutes=5),
            entries_scraped=8,
            entries_skipped=2,
            total_entries=108,
            queue_total=40,
            queue_processed=10,
            progress_updated_at=now - timedelta(seconds=1),
        )

        progress = Orchestrator._persisted_run_progress(
            run,
            total_entries=108,
            now=now,
        )

        self.assertEqual(progress["urls_total"], 40)
        self.assertEqual(progress["urls_processed"], 10)
        self.assertEqual(progress["progress_percent"], 25.0)
        self.assertEqual(progress["elapsed_seconds"], 300)
        self.assertEqual(progress["eta_seconds"], 900)
        self.assertEqual(
            progress["progress_updated_at"],
            "2026-08-12T12:04:59+00:00",
        )

    def test_calculate_entries_skipped_includes_prefiltered_lots(self) -> None:
        scraper = SimpleNamespace(
            stats={"urls_skipped_prefiltered": 4},
            _skipped_existing=4,
        )

        skipped = Orchestrator._calculate_entries_skipped(
            urls_processed=10,
            entries_scraped=7,
            scraper_instance=scraper,
        )

        self.assertEqual(skipped, 7)

    def test_prefiltered_skipped_count_uses_max_source(self) -> None:
        scraper = SimpleNamespace(
            stats={"urls_skipped_prefiltered": 5},
            _skipped_existing=2,
            prefiltered_skipped=3,
        )

        skipped = Orchestrator._calculate_entries_skipped(
            urls_processed=5,
            entries_scraped=5,
            scraper_instance=scraper,
        )

        self.assertEqual(skipped, 5)

    def test_calculate_entries_skipped_handles_non_numeric_values(self) -> None:
        scraper = SimpleNamespace(
            stats={"urls_skipped_prefiltered": "invalid"},
            _skipped_existing="3",
        )

        skipped = Orchestrator._calculate_entries_skipped(
            urls_processed="8",
            entries_scraped="10",
            scraper_instance=scraper,
        )

        self.assertEqual(skipped, 3)

    def test_merge_env_scraper_kwargs_injects_dorotheum_cookie(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "SMARTMATCH_DOROTHEUM_COOKIE_HEADER": "cf_clearance=abc; __cf_bm=def",
            },
            clear=False,
        ):
            merged = Orchestrator._merge_env_scraper_kwargs(
                scraper_name="dorotheum",
                scraper_kwargs={},
            )

        self.assertEqual(merged["cookie_header"], "cf_clearance=abc; __cf_bm=def")

    def test_merge_env_scraper_kwargs_keeps_explicit_cookie(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "SMARTMATCH_DOROTHEUM_COOKIE_HEADER": "cf_clearance=env",
            },
            clear=False,
        ):
            merged = Orchestrator._merge_env_scraper_kwargs(
                scraper_name="dorotheum",
                scraper_kwargs={
                    "cookie_header": "cf_clearance=explicit",
                },
            )

        self.assertEqual(merged["cookie_header"], "cf_clearance=explicit")

    def test_merge_env_scraper_kwargs_ignores_non_dorotheum(self) -> None:
        with patch.dict(
            "os.environ",
            {"SMARTMATCH_DOROTHEUM_COOKIE_HEADER": "cf_clearance=abc"},
            clear=False,
        ):
            merged = Orchestrator._merge_env_scraper_kwargs(
                scraper_name="christies",
                scraper_kwargs={"max_pages": 2},
            )

        self.assertEqual(merged, {"max_pages": 2})

    def test_request_cooldown_from_env_uses_scraper_env_var(self) -> None:
        with patch.dict(
            "os.environ",
            {"SCRAPER_REQUEST_COOLDOWN_SECONDS": "12.5"},
            clear=True,
        ):
            cooldown = Orchestrator._request_cooldown_from_env()

        self.assertEqual(cooldown, 12.5)

    def test_request_cooldown_from_env_defaults_when_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            cooldown = Orchestrator._request_cooldown_from_env()

        self.assertEqual(cooldown, Orchestrator.DEFAULT_REQUEST_COOLDOWN_SECONDS)

    def test_request_cooldown_from_env_rejects_invalid_value(self) -> None:
        with patch.dict(
            "os.environ",
            {"SCRAPER_REQUEST_COOLDOWN_SECONDS": "slow"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "SCRAPER_REQUEST_COOLDOWN_SECONDS"):
                Orchestrator._request_cooldown_from_env()

    def test_request_cooldown_from_env_rejects_negative_value(self) -> None:
        with patch.dict(
            "os.environ",
            {"SCRAPER_REQUEST_COOLDOWN_SECONDS": "-1"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "SCRAPER_REQUEST_COOLDOWN_SECONDS"):
                Orchestrator._request_cooldown_from_env()

    def test_guarded_run_skips_without_touching_active_process(self) -> None:
        orch = Orchestrator.__new__(Orchestrator)
        orch._lock = threading.Lock()
        orch._running = {}
        orch._active_runs = {}
        orch._engine = lambda: object()  # type: ignore[attr-defined]
        orch._run_scraper_sync = lambda *_args, **_kwargs: self.fail(  # type: ignore[attr-defined]
            "contended scraper must not run"
        )

        with (
            patch("scrapers.orchestrator.try_acquire_scraper_lock", return_value=None),
            patch("scrapers.orchestrator.time.sleep"),
        ):
            result = Orchestrator._run_scraper_guarded(orch, "christies")

        self.assertEqual(
            result,
            {
                "scraper": "christies",
                "status": "skipped",
                "reason": "already_running",
            },
        )

    def test_guarded_run_releases_lease_after_failure_result(self) -> None:
        orch = Orchestrator.__new__(Orchestrator)
        orch._lock = threading.Lock()
        orch._running = {}
        orch._active_runs = {}
        orch._engine = lambda: object()  # type: ignore[attr-defined]
        orch._finalize_interrupted_runs_for_scraper = lambda _name: None  # type: ignore[attr-defined]
        orch._run_scraper_sync = lambda *_args, **_kwargs: {  # type: ignore[attr-defined]
            "status": "failed"
        }

        lease = SimpleNamespace(release_calls=0)

        def release() -> None:
            lease.release_calls += 1

        lease.release = release
        with patch(
            "scrapers.orchestrator.try_acquire_scraper_lock",
            return_value=lease,
        ):
            result = Orchestrator._run_scraper_guarded(orch, "christies")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(lease.release_calls, 1)

    def test_run_sync_applies_dorotheum_env_kwargs(self) -> None:
        orch = Orchestrator.__new__(Orchestrator)
        orch._lock = threading.Lock()
        orch._running = {}
        orch._active_runs = {}

        session = _FakeSession()
        orch._session = lambda: session  # type: ignore[attr-defined]

        counts = iter([5, 5])
        orch._count_entries = lambda _session, _name: next(counts)  # type: ignore[attr-defined]
        orch._baseline_from_prior_run = lambda _session, _name, _run_id: 5  # type: ignore[attr-defined]

        captured_kwargs: dict[str, str] = {}

        class CapturingScraper:
            def __init__(self, **kwargs) -> None:
                captured_kwargs.update(kwargs)
                self.stats = {"urls_processed": 0}

            def run(self) -> None:
                return None

        orch._import_scraper_class = lambda _name: CapturingScraper  # type: ignore[attr-defined]

        with patch.dict(
            "os.environ",
            {
                "SMARTMATCH_DOROTHEUM_COOKIE_HEADER": "cf_clearance=abc",
            },
            clear=False,
        ):
            result = Orchestrator._run_scraper_sync(orch, "dorotheum")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(captured_kwargs["cookie_header"], "cf_clearance=abc")

    def test_completion_commit_failure_rolls_back_and_records_failed_run(self) -> None:
        orch = Orchestrator.__new__(Orchestrator)
        orch._lock = threading.Lock()
        orch._running = {}
        orch._active_runs = {}

        class RecoveringSession(_FakeSession):
            def commit(self) -> None:
                self.commits += 1
                if self.commits == 2:
                    raise RuntimeError("commit failed")

        session = RecoveringSession()
        orch._session = lambda: session  # type: ignore[attr-defined]
        counts = iter([5, 6, 6])
        orch._count_entries = lambda _session, _name: next(counts)  # type: ignore[attr-defined]
        orch._baseline_from_prior_run = lambda _session, _name, _run_id: 5  # type: ignore[attr-defined]

        class SuccessfulScraper:
            def __init__(self, **_kwargs) -> None:
                self.stats = {"urls_processed": 1}

            def run(self) -> None:
                return None

        orch._import_scraper_class = lambda _name: SuccessfulScraper  # type: ignore[attr-defined]

        result = Orchestrator._run_scraper_sync(orch, "christies")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(session.rollbacks, 1)
        self.assertEqual(session.commits, 3)
        self.assertEqual(session.added[0].status, "failed")

    def test_failed_run_records_partial_entries_scraped(self) -> None:
        orch = Orchestrator.__new__(Orchestrator)
        orch._lock = threading.Lock()
        orch._running = {}
        orch._active_runs = {}

        session = _FakeSession()
        orch._session = lambda: session  # type: ignore[attr-defined]

        counts = iter([100, 107])
        orch._count_entries = lambda _session, _name: next(counts)  # type: ignore[attr-defined]
        orch._baseline_from_prior_run = lambda _session, _name, _run_id: 100  # type: ignore[attr-defined]

        class FailingScraper:
            def __init__(self, **_kwargs) -> None:
                self.stats = {"urls_processed": 12}
                self._skipped_existing = 2

            def run(self) -> None:
                raise RuntimeError("boom")

        orch._import_scraper_class = lambda _name: FailingScraper  # type: ignore[attr-defined]

        result = Orchestrator._run_scraper_sync(orch, "sothebys")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["entries_scraped"], 7)
        self.assertEqual(result["entries_skipped"], 7)
        self.assertEqual(result["total_entries"], 107)

        run = session.added[0]
        self.assertEqual(run.entries_scraped, 7)
        self.assertEqual(run.entries_skipped, 7)
        self.assertEqual(run.total_entries, 107)
        self.assertEqual(run.status, "failed")
        self.assertEqual(session.commits, 2)
        self.assertTrue(session.closed)

    def test_failed_run_uses_safe_fallback_when_recount_fails(self) -> None:
        orch = Orchestrator.__new__(Orchestrator)
        orch._lock = threading.Lock()
        orch._running = {}
        orch._active_runs = {}

        session = _FakeSession()
        orch._session = lambda: session  # type: ignore[attr-defined]

        calls = {"count": 0}

        def _count_entries(_session, _name):
            calls["count"] += 1
            if calls["count"] == 1:
                return 20
            raise RuntimeError("count failed")

        orch._count_entries = _count_entries  # type: ignore[attr-defined]

        class FailingScraper:
            def __init__(self, **_kwargs) -> None:
                self.stats = {"urls_processed": 3}
                self._skipped_existing = 1

            def run(self) -> None:
                raise RuntimeError("boom")

        orch._import_scraper_class = lambda _name: FailingScraper  # type: ignore[attr-defined]

        result = Orchestrator._run_scraper_sync(orch, "dorotheum")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["entries_scraped"], 0)
        self.assertEqual(result["entries_skipped"], 4)
        self.assertEqual(result["total_entries"], 20)

        run = session.added[0]
        self.assertEqual(run.entries_scraped, 0)
        self.assertEqual(run.entries_skipped, 4)
        self.assertEqual(run.total_entries, 20)


if __name__ == "__main__":
    unittest.main()
