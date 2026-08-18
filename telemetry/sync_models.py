"""Structural settings and immutable synchronization results."""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol


class SyncSettings(Protocol):
    endpoint: str
    timeout_seconds: float
    auth_token: str
    page_delay_min_seconds: float
    page_delay_max_seconds: float


@dataclass(frozen=True)
class RawPage:
    path: Path
    content_sha256: str
    counts: Mapping[str, int]
    workspace_bytes: int = 0


@dataclass(frozen=True)
class EncodedSyncPage:
    body: bytes
    uncompressed_sha256: str
    uncompressed_bytes: int


@dataclass(frozen=True)
class SyncDeliveryResult:
    sync_id: str
    page_count: int
    total_compressed_bytes: int
    operation_sha256: str
    last_page: EncodedSyncPage
