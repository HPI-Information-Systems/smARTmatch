from __future__ import annotations

import unittest

from scrapers.run_lock import (
    SCRAPER_LOCK_KEYS,
    ScraperLease,
    try_acquire_scraper_lock,
)


class _Result:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar(self) -> bool:
        return self.value


class _Connection:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.calls = []
        self.closed = False
        self.invalidated = False

    def execution_options(self, **_kwargs):
        return self

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        if (
            "pg_try_advisory_lock" in str(statement)
            or "pg_advisory_unlock" in str(statement)
            or "FROM pg_locks" in str(statement)
        ):
            return _Result(self.acquired)
        return _Result(True)

    def close(self) -> None:
        self.closed = True

    def invalidate(self) -> None:
        self.invalidated = True


class _Engine:
    def __init__(self, acquired: bool) -> None:
        self.connection = _Connection(acquired)
        self.disposed = False

    def connect(self) -> _Connection:
        return self.connection

    def dispose(self) -> None:
        self.disposed = True


class ScraperLockTests(unittest.TestCase):
    def test_lock_keys_are_stable_and_unique(self) -> None:
        self.assertEqual(SCRAPER_LOCK_KEYS["christies"], 1)
        self.assertEqual(len(SCRAPER_LOCK_KEYS), len(set(SCRAPER_LOCK_KEYS.values())))

    def test_acquired_lock_is_released_and_connection_closed(self) -> None:
        engine = _Engine(acquired=True)

        lease = try_acquire_scraper_lock(engine, "christies")

        self.assertIsInstance(lease, ScraperLease)
        lease.release()
        self.assertTrue(engine.connection.closed)
        self.assertTrue(engine.disposed)
        self.assertIn("pg_advisory_unlock", engine.connection.calls[-1][0])

    def test_lease_can_verify_its_backend_still_owns_lock(self) -> None:
        engine = _Engine(acquired=True)
        lease = try_acquire_scraper_lock(engine, "christies")

        lease.ensure_held()

        self.assertIn("FROM pg_locks", engine.connection.calls[-1][0])
        lease.release()

    def test_lease_fences_worker_after_backend_lock_loss(self) -> None:
        engine = _Engine(acquired=True)
        lease = try_acquire_scraper_lock(engine, "christies")
        engine.connection.acquired = False

        with self.assertRaisesRegex(RuntimeError, "Lost advisory lock"):
            lease.ensure_held()

        # Simulate connection teardown; release correctly reports lost ownership.
        with self.assertRaisesRegex(RuntimeError, "already lost"):
            lease.release()
        self.assertTrue(engine.connection.invalidated)

    def test_unavailable_lock_returns_none_without_waiting(self) -> None:
        engine = _Engine(acquired=False)

        lease = try_acquire_scraper_lock(engine, "sothebys")

        self.assertIsNone(lease)
        self.assertTrue(engine.connection.closed)
        self.assertTrue(engine.disposed)

    def test_unknown_scraper_is_rejected_before_connecting(self) -> None:
        engine = _Engine(acquired=True)

        with self.assertRaisesRegex(ValueError, "No advisory-lock key"):
            try_acquire_scraper_lock(engine, "unknown")

        self.assertFalse(engine.connection.closed)


if __name__ == "__main__":
    unittest.main()
