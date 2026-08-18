"""Three-step, paginated database replication over telemetry HTTP events."""

# flake8: noqa

# Existing callers may keep importing telemetry.telemetry_sync; cohesive modules
# below own the implementation.
import gzip
import hashlib
import random
import tempfile
import time
from urllib.request import build_opener
from uuid import uuid4

from telemetry.sync_budget import (
    TransferBudget,
    WorkspaceBudget,
    _ClosureMaterializationBudget,
    _directory_size,
)
from telemetry.sync_catalog import (
    SyncCatalog,
    _parse_needed_identifiers,
    _validate_needed_acknowledgement,
)
from telemetry.sync_codec import (
    _check_transfer_budget,
    _inventory_counts,
    _operation_hash,
    _page_envelope,
    _preflight_phase_pages,
    _write_raw_page,
    encode_sync_page,
)
from telemetry.sync_constants import (
    _LEGACY_STALE_SPOOL_SECONDS,
    _MATERIALIZATION_FIXED_OVERHEAD_BYTES,
    _MATERIALIZATION_ROW_OVERHEAD_BYTES,
    _PAGE_ENVELOPE_RESERVE_BYTES,
    _SYNC_SPOOL_DIRECTORY,
    ASYNC_APPLY_POLL_INTERVAL_SECONDS,
    ASYNC_APPLY_TIMEOUT_SECONDS,
    ASYNC_POLL_REQUEST_TIMEOUT_SECONDS,
    DATA_MATCHES_PER_PAGE,
    INVENTORY_MATCHES_PER_PAGE,
    MAX_ACKNOWLEDGEMENT_BYTES,
    MAX_COMPRESSED_PAGE_BYTES,
    MAX_RETRY_AFTER_SECONDS,
    MAX_SYNC_OPERATION_PAGES,
    MAX_SYNC_TRANSFER_BYTES,
    MAX_SYNC_WORKSPACE_BYTES,
    MAX_UNCOMPRESSED_PAGE_BYTES,
    MIN_SYNC_FILESYSTEM_FREE_BYTES,
    PAGE_RETRIES,
    SYNC_SCHEMA_VERSION,
    TARGET_UNCOMPRESSED_PAGE_BYTES,
    logger,
)
from telemetry.sync_data import (
    _split_data_content,
    _spool_data_content,
    _spool_data_pages,
    _spool_requested_match_pairs,
)
from telemetry.sync_delivery import deliver_sync_operation
from telemetry.sync_errors import (
    AsyncApplyError,
    SourceSnapshotChanged,
    SyncError,
    SyncHttpError,
    SyncProtocolError,
    SyncWorkspaceLimitError,
    TerminalSyncError,
    TransientSyncError,
    UnsendableClosureError,
    _ClosureMaterializationLimit,
    _RejectRedirects,
    is_terminal_sync_failure,
    is_transient_sync_failure,
)
from telemetry.sync_graph import _build_data_content, _replication_graph_hashes
from telemetry.sync_http import (
    _get_operation_status,
    _operation_status_url,
    _post_page,
    _post_page_with_retries,
    _retry_after_seconds,
    _sleep_before_next_page,
    _wait_for_async_apply,
)
from telemetry.sync_inventory import (
    _add_inventory_hashes,
    _fetch_inventory_rows,
    _spool_inventory_pages,
)
from telemetry.sync_models import (
    EncodedSyncPage,
    RawPage,
    SyncDeliveryResult,
    SyncSettings,
)
from telemetry.sync_queries import (
    _fetch_entities,
    _fetch_integer_entities,
    _fetch_link_rows,
    _fetch_requested_match_rows,
    _reserve_query_rows,
    _snapshot_connection,
)
from telemetry.sync_utils import (
    _as_utc,
    _canonical_hash,
    _canonical_json,
    _canonical_uuid,
    _isoformat,
    _json_safe,
    _match_key,
    _values,
)
from telemetry.sync_workspace import (
    cleanup_stale_sync_spools,
    sync_spool_root,
    sync_workspace,
)
