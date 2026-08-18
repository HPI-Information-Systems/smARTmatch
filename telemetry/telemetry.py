"""Bounded, opt-in daily telemetry for the matching pipeline."""

# flake8: noqa

# Keep the established import and module-execution surface while implementation
# lives in focused modules.
import hashlib
import os
import signal
import subprocess
import sys
import time

from matching_pipeline.shared.db import connect
from matching_pipeline.shared.env import env_bool, env_str
from telemetry.build_provenance import GIT_SOURCE as BUILD_PROVENANCE_GIT_SOURCE
from telemetry.build_provenance import SCHEMA_VERSION as BUILD_PROVENANCE_SCHEMA_VERSION
from telemetry.build_provenance import (
    SOURCE_HASH_ALGORITHM,
    SOURCE_PATHS,
    source_snapshot,
)
from telemetry.cli import _parse_args, _run_one_shot, main
from telemetry.config import (
    _duration_seconds,
    _is_local_telemetry_host,
    _nonnegative_float_setting,
    _telemetry_enabled,
    load_telemetry_settings,
)
from telemetry.constants import (
    _MAX_REPORTED_SUBDIRECTORIES,
    _MAX_TREE_ORDERING_BYTES,
    DAEMON_POLL_SECONDS,
    DEFAULT_PAGE_DELAY_MAX_SECONDS,
    DEFAULT_PAGE_DELAY_MIN_SECONDS,
    DEFAULT_PROCESS_DEADLINE_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    REMOTE_TELEMETRY_HOST,
    STARTUP_MAX_RETRY_SECONDS,
    STARTUP_MAX_TRANSIENT_FAILURES,
    TELEMETRY_MODULE,
    TELEMETRY_SCHEMA_VERSION,
    TELEMETRY_WINDOW_DAYS,
    WORKER_DEADLINE_CLEANUP_GRACE_SECONDS,
    WORKER_EXIT_DEADLINE,
    WORKER_EXIT_NOOP,
    WORKER_EXIT_TERMINAL,
    WORKER_EXIT_TRANSIENT,
    WORKER_LAUNCH_RETRY_SECONDS,
    logger,
)
from telemetry.daemon import (
    _launch_worker,
    _stop_worker,
    _wait_with_health,
    run_telemetry_daemon,
)
from telemetry.database import (
    _LATEST_APPLIED_MIGRATION_SQL,
    _claim_daily_attempt,
    _collect_database_snapshot,
    _hash_database_query,
    _latest_applied_migration,
    _record_daily_result,
    _record_daily_sync_result,
)
from telemetry.delivery import try_send_daily_telemetry, try_send_startup_telemetry
from telemetry.models import (
    DirectoryHash,
    NonDatabaseTelemetrySnapshot,
    TelemetryDeadlineExceeded,
    TelemetrySettings,
    TreeHashSnapshot,
)
from telemetry.provenance import (
    _build_provenance_identity,
    _git_identity,
    _requirement_lock_metadata,
    _runtime_reproducibility_metadata,
    _runtime_source_hash,
    _strict_optional_bool,
)
from telemetry.serialization import (
    _as_utc,
    _column_names,
    _fetchall_dicts,
    _fetchone_dict,
    _isoformat,
    _json_safe,
)
from telemetry.summary import (
    _collect_non_database_telemetry,
    _tree_snapshot_payload,
    collect_telemetry_payload,
)
from telemetry.sync_delivery import deliver_sync_operation
from telemetry.sync_errors import is_terminal_sync_failure
from telemetry.sync_models import SyncDeliveryResult
from telemetry.tree_hashing import (
    _check_tree_ordering_budget,
    _externally_sorted_directory_names,
    _hash_file,
    _path_size_file_hash,
    _tree_record,
    hash_directory_tree,
)

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
