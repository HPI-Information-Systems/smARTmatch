"""Value objects and control-flow signals for telemetry collection."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from telemetry.constants import (
    DEFAULT_PAGE_DELAY_MAX_SECONDS,
    DEFAULT_PAGE_DELAY_MIN_SECONDS,
)


class TelemetryDeadlineExceeded(BaseException):
    pass


@dataclass(frozen=True)
class TelemetrySettings:
    endpoint: str
    auth_token: str
    image_root: Path
    timeout_seconds: float
    match_expiration_seconds: int
    page_delay_min_seconds: float = DEFAULT_PAGE_DELAY_MIN_SECONDS
    page_delay_max_seconds: float = DEFAULT_PAGE_DELAY_MAX_SECONDS


@dataclass(frozen=True)
class DirectoryHash:
    sha256: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class TreeHashSnapshot:
    root: DirectoryHash
    subdirectories: Mapping[str, DirectoryHash]
    subdirectory_count: int
    subdirectories_truncated: bool
    file_hashes: Mapping[str, str]
    hash_basis: str
    error_count: int
    errors: Sequence[str]


@dataclass(frozen=True)
class NonDatabaseTelemetrySnapshot:
    images: TreeHashSnapshot
    git: Mapping[str, Any]
    runtime: Mapping[str, Any]
