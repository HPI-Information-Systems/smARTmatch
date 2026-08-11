"""PostgreSQL advisory locks for cross-process scraper coordination."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

# The two-int PostgreSQL advisory-lock API gives this application a namespace
# and every scraper a stable key without relying on Python's randomized hash().
SCRAPER_LOCK_NAMESPACE = 1_397_573_100
SCRAPER_LOCK_KEYS: dict[str, int] = {
    "christies": 1,
    "sothebys": 2,
    "drouot": 3,
    "lottissimo": 4,
    "dorotheum": 5,
    "lostart": 6,
}


@dataclass
class ScraperLease:
    """A session-level PostgreSQL advisory lock held on one connection."""

    scraper_name: str
    connection: Connection
    engine: Engine
    _released: bool = False

    def ensure_held(self) -> None:
        """Raise if this worker's dedicated backend no longer owns the lock."""
        held = bool(
            self.connection.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_locks
                        WHERE locktype = 'advisory'
                          AND pid = pg_backend_pid()
                          AND granted
                          AND CAST(classid AS bigint) = :namespace
                          AND CAST(objid AS bigint) = :scraper_key
                          AND objsubid = 2
                    )
                    """
                ),
                {
                    "namespace": SCRAPER_LOCK_NAMESPACE,
                    "scraper_key": SCRAPER_LOCK_KEYS[self.scraper_name],
                },
            ).scalar()
        )
        if not held:
            raise RuntimeError(
                f"Lost advisory lock for scraper {self.scraper_name!r}; stopping worker"
            )

    def release(self) -> None:
        """Release and close the dedicated lock connection exactly once."""
        if self._released:
            return
        self._released = True
        try:
            unlocked = bool(
                self.connection.execute(
                    text("SELECT pg_advisory_unlock(:namespace, :scraper_key)"),
                    {
                        "namespace": SCRAPER_LOCK_NAMESPACE,
                        "scraper_key": SCRAPER_LOCK_KEYS[self.scraper_name],
                    },
                ).scalar()
            )
            if not unlocked:
                raise RuntimeError(
                    f"Advisory lock for scraper {self.scraper_name!r} was already lost"
                )
        except Exception:
            # Never return a connection with a possibly-held session lock to a
            # pool. Invalidation closes the underlying DBAPI connection.
            try:
                self.connection.invalidate()
            except Exception:
                pass
            raise
        finally:
            self.connection.close()
            self.engine.dispose()

    def __enter__(self) -> "ScraperLease":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.release()


def try_acquire_scraper_lock(engine: Engine, scraper_name: str) -> ScraperLease | None:
    """Return a lease when ``scraper_name`` is idle, otherwise return ``None``."""
    if scraper_name not in SCRAPER_LOCK_KEYS:
        raise ValueError(f"No advisory-lock key registered for scraper {scraper_name!r}")

    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        # A scraper can legitimately run for hours. Do not let a server-level
        # idle-session timeout silently remove its ownership lock.
        connection.execute(text("SET idle_session_timeout = 0"))
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:namespace, :scraper_key)"),
                {
                    "namespace": SCRAPER_LOCK_NAMESPACE,
                    "scraper_key": SCRAPER_LOCK_KEYS[scraper_name],
                },
            ).scalar()
        )
    except Exception:
        connection.close()
        engine.dispose()
        raise

    if not acquired:
        connection.close()
        engine.dispose()
        return None

    return ScraperLease(
        scraper_name=scraper_name,
        connection=connection,
        engine=engine,
    )
