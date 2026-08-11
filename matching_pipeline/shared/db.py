"""PostgreSQL connection helpers shared by all matching stages."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg

from matching_pipeline.shared.env import env_int, env_required_str, env_str

_CONNECTION_URL_ENVS = ("SMARTMATCH_DATABASE_URL", "DATABASE_URL")
_REQUIRED_POSTGRES_ENVS = (
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
)


def connect_db():
    """Connect using the validated image-stage environment contract."""
    kwargs: dict[str, object] = {
        "dbname": env_required_str("POSTGRES_DB"),
        "user": env_required_str("POSTGRES_USER"),
        "password": env_required_str("POSTGRES_PASSWORD"),
    }
    socket_dir = env_str("POSTGRES_SOCKET_DIR")
    if socket_dir and (Path(socket_dir) / ".s.PGSQL.5432").exists():
        kwargs["host"] = socket_dir
    else:
        port = env_int("POSTGRES_PORT")
        if port is None:
            raise ValueError("Environment variable POSTGRES_PORT is required")
        if port <= 0 or port > 65_535:
            raise ValueError("Environment variable POSTGRES_PORT must be in [1, 65535]")
        kwargs["host"] = env_required_str("POSTGRES_HOST")
        kwargs["port"] = port
    return psycopg.connect(**kwargs)


def _non_empty_env(name: str) -> str | None:
    value = os.getenv(name)
    return value if value else None


def _connection_url() -> str | None:
    for name in _CONNECTION_URL_ENVS:
        value = _non_empty_env(name)
        if value:
            return value
    return None


def get_connection_kwargs() -> dict[str, str | int]:
    """Return metadata-stage connection kwargs from required POSTGRES_* values."""
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in _REQUIRED_POSTGRES_ENVS:
        value = _non_empty_env(name)
        if value is None:
            missing.append(name)
        else:
            values[name] = value
    if missing:
        required = ", ".join(_REQUIRED_POSTGRES_ENVS)
        raise RuntimeError(
            f"Missing database environment variables: {', '.join(missing)}. "
            "Set SMARTMATCH_DATABASE_URL/DATABASE_URL or all POSTGRES_* "
            f"variables ({required})."
        )
    try:
        port = int(values["POSTGRES_PORT"])
    except ValueError as exc:
        raise ValueError("Environment variable POSTGRES_PORT must be an integer") from exc
    return {
        "host": values["POSTGRES_HOST"],
        "port": port,
        "dbname": values["POSTGRES_DB"],
        "user": values["POSTGRES_USER"],
        "password": values["POSTGRES_PASSWORD"],
    }


def connect(**kwargs: Any) -> psycopg.Connection:
    """Connect using a database URL or the metadata-stage POSTGRES_* contract."""
    url = _connection_url()
    if url:
        return psycopg.connect(url, **kwargs)
    connection_kwargs: dict[str, Any] = get_connection_kwargs()
    connection_kwargs.update(kwargs)
    return psycopg.connect(**connection_kwargs)
