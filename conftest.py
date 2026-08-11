"""Repository-wide test isolation from developer and production settings."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_postgres_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide non-routable placeholders to tests that construct DB clients."""
    values = {
        "POSTGRES_HOST": "test-db.invalid",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "smartmatch_test",
        "POSTGRES_USER": "smartmatch_test",
        "POSTGRES_PASSWORD": "smartmatch_test",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
