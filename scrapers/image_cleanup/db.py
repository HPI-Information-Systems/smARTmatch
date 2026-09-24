"""PostgreSQL connection for auction-image cleanup."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg


def connect_db():
    """Connect with the scraper service's POSTGRES_* settings."""
    kwargs: dict[str, object] = {
        "dbname": _required("POSTGRES_DB"),
        "user": _required("POSTGRES_USER"),
        "password": _required("POSTGRES_PASSWORD"),
    }
    socket_dir = _optional("POSTGRES_SOCKET_DIR")
    if socket_dir and (Path(socket_dir) / ".s.PGSQL.5432").exists():
        kwargs["host"] = socket_dir
        return psycopg.connect(**kwargs)
    port_text = _required("POSTGRES_PORT")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("Environment variable POSTGRES_PORT must be an integer") from exc
    if port <= 0 or port > 65_535:
        raise ValueError("Environment variable POSTGRES_PORT must be in [1, 65535]")
    kwargs["host"] = _required("POSTGRES_HOST")
    kwargs["port"] = port
    return psycopg.connect(**kwargs)


def image_root_from_env() -> Path:
    return Path(_required("SMARTMATCH_IMAGES_DIR")).expanduser().resolve()


def _optional(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _required(name: str) -> str:
    value = _optional(name)
    if value is None:
        raise ValueError(f"Environment variable {name} is required")
    return value
