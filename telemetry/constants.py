"""Constants shared by telemetry sender modules."""

import logging
import re
from pathlib import Path

logger = logging.getLogger("telemetry.telemetry")

TELEMETRY_SCHEMA_VERSION = 2
TELEMETRY_WINDOW_DAYS = 7
TELEMETRY_MODULE = "telemetry.telemetry"
REMOTE_TELEMETRY_HOST = "smartmatch.leogruetzner.com"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PAGE_DELAY_MIN_SECONDS = 0.25
DEFAULT_PAGE_DELAY_MAX_SECONDS = 0.5
DEFAULT_PROCESS_DEADLINE_SECONDS = 2 * 60 * 60
DAEMON_POLL_SECONDS = 1.0
WORKER_LAUNCH_RETRY_SECONDS = 30.0
STARTUP_MAX_TRANSIENT_FAILURES = 5
STARTUP_MAX_RETRY_SECONDS = 10 * 60.0
WORKER_EXIT_TRANSIENT = 75
WORKER_EXIT_TERMINAL = 78
WORKER_EXIT_NOOP = 79
WORKER_EXIT_DEADLINE = 124
WORKER_DEADLINE_CLEANUP_GRACE_SECONDS = 30.0
_HASH_CHUNK_BYTES = 1024 * 1024
_HASH_FETCH_ROWS = 1_000
_MAX_HASH_ERRORS = 20
_MAX_REPORTED_SUBDIRECTORIES = 1_024
_MAX_TREE_ORDERING_BYTES = 512 * 1024 * 1024
_MIN_TREE_ORDERING_FREE_BYTES = 256 * 1024 * 1024
_MAX_BUILD_PROVENANCE_BYTES = 64 * 1024
_GIT_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REQUIREMENT_PIN_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^]]+\])?==([^\s;]+)"
)
_COMPONENT_REQUIREMENT_FILES = {
    "application": Path("requirements.txt"),
    "matching_pipeline": Path("matching_pipeline/requirements.txt"),
}
_REPRODUCIBILITY_PACKAGES = (
    "torch",
    "transformers",
    "kornia",
    "scikit-learn",
    "pyarrow",
)

_DB_ROW_HASH_PREFIX = b"smartmatch-db-rows-v1\0"
_CONTENT_TREE_HASH_PREFIX = b"smartmatch-content-tree-v1\0"
_PATH_SIZE_TREE_HASH_PREFIX = b"smartmatch-path-size-tree-v1\0"
_PATH_SIZE_FILE_HASH_PREFIX = b"smartmatch-path-size-file-v1\0"
