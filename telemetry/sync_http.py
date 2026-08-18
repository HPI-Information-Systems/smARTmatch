"""HTTP page delivery, acknowledgements, retries, and async polling."""

import json
import math
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, build_opener

from telemetry.sync_budget import TransferBudget
from telemetry.sync_constants import (
    ASYNC_APPLY_POLL_INTERVAL_SECONDS,
    ASYNC_APPLY_TIMEOUT_SECONDS,
    ASYNC_POLL_REQUEST_TIMEOUT_SECONDS,
    MAX_ACKNOWLEDGEMENT_BYTES,
    MAX_RETRY_AFTER_SECONDS,
    PAGE_RETRIES,
    logger,
)
from telemetry.sync_errors import (
    AsyncApplyError,
    SyncHttpError,
    SyncProtocolError,
    _RejectRedirects,
    is_transient_sync_failure,
)
from telemetry.sync_models import EncodedSyncPage, SyncSettings


def _sleep_before_next_page(settings: SyncSettings) -> float:
    """Apply bounded jitter after an acknowledgement to avoid request bursts."""
    minimum = float(getattr(settings, "page_delay_min_seconds", 0.0))
    maximum = float(getattr(settings, "page_delay_max_seconds", 0.0))
    if not math.isfinite(minimum) or minimum < 0:
        raise ValueError("Telemetry page delay minimum must be finite and nonnegative")
    if not math.isfinite(maximum) or maximum < minimum:
        raise ValueError(
            "Telemetry page delay maximum must be finite and at least the minimum"
        )
    if maximum == 0:
        return 0.0
    delay = random.uniform(minimum, maximum)
    logger.debug("Telemetry page pacing delay_seconds=%.3f", delay)
    time.sleep(delay)
    return delay


def _post_page_with_retries(
    settings: SyncSettings,
    encoded: EncodedSyncPage,
    *,
    sync_id: str,
    phase: str,
    page_number: int,
    page_count: int,
    transfer_budget: TransferBudget | None = None,
) -> dict[str, Any]:
    transfer_budget = transfer_budget or TransferBudget()
    for attempt in range(1, PAGE_RETRIES + 1):
        try:
            transfer_budget.debit(len(encoded.body))
            return _post_page(
                settings,
                encoded,
                sync_id=sync_id,
                phase=phase,
                page_number=page_number,
                page_count=page_count,
            )
        except Exception as exc:
            retryable = is_transient_sync_failure(exc)
            if not retryable or attempt == PAGE_RETRIES:
                logger.exception(
                    "Telemetry page delivery failed sync_id=%s phase=%s page=%d/%d "
                    "attempt=%d/%d retryable=%s",
                    sync_id,
                    phase,
                    page_number + 1,
                    page_count,
                    attempt,
                    PAGE_RETRIES,
                    retryable,
                )
                raise
            retry_delay = min(2 ** (attempt - 1), 30)
            if isinstance(exc, SyncHttpError) and exc.retry_after_seconds is not None:
                retry_delay = max(
                    retry_delay,
                    min(exc.retry_after_seconds, MAX_RETRY_AFTER_SECONDS),
                )
            logger.warning(
                "Telemetry page delivery retry sync_id=%s phase=%s page=%d/%d "
                "attempt=%d/%d retry_seconds=%d error=%s",
                sync_id,
                phase,
                page_number + 1,
                page_count,
                attempt,
                PAGE_RETRIES,
                retry_delay,
                type(exc).__name__,
            )
            time.sleep(retry_delay)
    raise AssertionError("unreachable")


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def _post_page(
    settings: SyncSettings,
    encoded: EncodedSyncPage,
    *,
    sync_id: str,
    phase: str,
    page_number: int,
    page_count: int,
    prefer_async: bool = True,
) -> dict[str, Any]:
    auth_token = str(getattr(settings, "auth_token", "") or "").strip()
    if not auth_token:
        raise ValueError("Telemetry sync requires an authentication token")
    async_final = prefer_async and phase == "data" and page_number == page_count - 1
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
        "Accept": "application/json",
        "User-Agent": "smARTmatch-telemetry-sync/3",
        "X-Smartmatch-Sync-ID": sync_id,
        "X-Smartmatch-Phase": phase,
        "X-Smartmatch-Page-Number": str(page_number),
        "X-Smartmatch-Page-Count": str(page_count),
        "X-Uncompressed-Content-SHA256": encoded.uncompressed_sha256,
        "X-Uncompressed-Content-Length": str(encoded.uncompressed_bytes),
        "X-Compressed-Content-Length": str(len(encoded.body)),
        "Idempotency-Key": (
            f"smartmatch-sync-{sync_id}-{phase}-{page_number}-"
            f"{encoded.uncompressed_sha256[:24]}"
        ),
    }
    if async_final:
        headers["Prefer"] = "respond-async"
    request = Request(
        settings.endpoint, data=encoded.body, headers=headers, method="POST"
    )
    opener = build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=settings.timeout_seconds) as response:
            status = int(response.status)
            response_body = response.read(MAX_ACKNOWLEDGEMENT_BYTES + 1)
    except HTTPError as exc:
        try:
            detail = exc.read(16 * 1024).decode("utf-8", errors="replace")
        except Exception:
            detail = None
        raise SyncHttpError(
            int(exc.code),
            detail,
            retry_after_seconds=_retry_after_seconds(
                exc.headers.get("Retry-After") if exc.headers is not None else None
            ),
        ) from exc
    if status < 200 or status >= 300:
        raise SyncHttpError(status)
    if len(response_body) > MAX_ACKNOWLEDGEMENT_BYTES:
        raise SyncProtocolError(
            "Telemetry receiver acknowledgement exceeds the fixed size limit"
        )
    try:
        acknowledgement = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncProtocolError(
            "Telemetry receiver returned an invalid acknowledgement"
        ) from exc
    if not isinstance(acknowledgement, Mapping):
        raise SyncProtocolError("Telemetry acknowledgement must be an object")
    expected = {
        "sync_id": sync_id,
        "phase": phase,
        "page_number": page_number,
        "payload_sha256": encoded.uncompressed_sha256,
    }
    for key, value in expected.items():
        actual = acknowledgement.get(key)
        if type(actual) is not type(value) or actual != value:
            raise SyncProtocolError(
                f"Telemetry acknowledgement mismatch for {key}: "
                f"expected {value!r}, got {actual!r}"
            )
    expected_complete = phase == "data" and page_number == page_count - 1
    async_accepted = (
        async_final
        and status == 202
        and acknowledgement.get("accepted") is True
        and acknowledgement.get("status") == "applying"
    )
    if async_accepted:
        if acknowledgement.get("complete") is not False:
            raise SyncProtocolError(
                "Asynchronous telemetry acknowledgement cannot be complete"
            )
        if acknowledgement.get("poll_after_seconds") != 60:
            raise SyncProtocolError(
                "Telemetry receiver returned an invalid polling interval"
            )
    elif acknowledgement.get("complete") is not expected_complete:
        raise SyncProtocolError(
            "Telemetry acknowledgement has an invalid complete flag"
        )
    if phase == "inventory" and not isinstance(acknowledgement.get("needed"), Mapping):
        raise SyncProtocolError(
            "Inventory acknowledgement must contain a needed object"
        )
    return dict(acknowledgement)


def _operation_status_url(endpoint: str, sync_id: str) -> str:
    parsed = urlsplit(endpoint)
    suffix = "/pages"
    if not parsed.path.endswith(suffix):
        raise ValueError("Telemetry endpoint must end with /pages")
    path = parsed.path[: -len(suffix)] + "/operations/" + quote(sync_id, safe="")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _get_operation_status(
    settings: SyncSettings,
    *,
    sync_id: str,
    operation_sha256: str,
    request_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    auth_token = str(getattr(settings, "auth_token", "") or "").strip()
    if not auth_token:
        raise ValueError("Telemetry sync requires an authentication token")
    request = Request(
        _operation_status_url(settings.endpoint, sync_id),
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Accept": "application/json",
            "User-Agent": "smARTmatch-telemetry-sync/3",
        },
        method="GET",
    )
    opener = build_opener(_RejectRedirects())
    request_timeout = min(
        float(settings.timeout_seconds),
        ASYNC_POLL_REQUEST_TIMEOUT_SECONDS,
        (
            request_timeout_seconds
            if request_timeout_seconds is not None
            else ASYNC_POLL_REQUEST_TIMEOUT_SECONDS
        ),
    )
    try:
        with opener.open(request, timeout=request_timeout) as response:
            status_code = int(response.status)
            response_body = response.read(MAX_ACKNOWLEDGEMENT_BYTES + 1)
    except HTTPError as exc:
        try:
            detail = exc.read(16 * 1024).decode("utf-8", errors="replace")
        except Exception:
            detail = None
        raise SyncHttpError(
            int(exc.code),
            detail,
            retry_after_seconds=_retry_after_seconds(
                exc.headers.get("Retry-After") if exc.headers is not None else None
            ),
        ) from exc
    if status_code < 200 or status_code >= 300:
        raise SyncHttpError(status_code)
    if len(response_body) > MAX_ACKNOWLEDGEMENT_BYTES:
        raise SyncProtocolError(
            "Telemetry operation status exceeds the fixed size limit"
        )
    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncProtocolError(
            "Telemetry receiver returned an invalid operation status"
        ) from exc
    if not isinstance(result, Mapping) or result.get("sync_id") != sync_id:
        raise SyncProtocolError("Telemetry operation status has an invalid sync ID")
    operation_status = result.get("status")
    if operation_status not in {"applying", "complete", "superseded", "failed"}:
        raise SyncProtocolError("Telemetry operation status is invalid")
    expected_complete = operation_status in {"complete", "superseded"}
    expected_failed = operation_status == "failed"
    if result.get("complete") is not expected_complete:
        raise SyncProtocolError("Telemetry operation completion state is invalid")
    if result.get("failed") is not expected_failed:
        raise SyncProtocolError("Telemetry operation failure state is invalid")
    if expected_complete and result.get("operation_sha256") != operation_sha256:
        raise SyncProtocolError("Telemetry operation digest does not match")
    return dict(result)


def _wait_for_async_apply(
    settings: SyncSettings,
    *,
    sync_id: str,
    operation_sha256: str,
) -> dict[str, Any]:
    started_at = time.monotonic()
    deadline = started_at + ASYNC_APPLY_TIMEOUT_SECONDS
    poll_count = (ASYNC_APPLY_TIMEOUT_SECONDS // ASYNC_APPLY_POLL_INTERVAL_SECONDS) + 1
    for poll_number in range(1, poll_count + 1):
        scheduled_at = started_at + (
            (poll_number - 1) * ASYNC_APPLY_POLL_INTERVAL_SECONDS
        )
        sleep_seconds = scheduled_at - time.monotonic()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        remaining_seconds = deadline - time.monotonic()
        result = None
        try:
            result = _get_operation_status(
                settings,
                sync_id=sync_id,
                operation_sha256=operation_sha256,
                request_timeout_seconds=(
                    remaining_seconds
                    if remaining_seconds > 0
                    else ASYNC_POLL_REQUEST_TIMEOUT_SECONDS
                ),
            )
        except Exception as exc:
            if not is_transient_sync_failure(exc):
                raise
            logger.warning(
                "Telemetry apply status poll failed sync_id=%s poll=%d/%d error=%s",
                sync_id,
                poll_number,
                poll_count,
                type(exc).__name__,
            )
        if result is not None:
            if result["complete"]:
                logger.info(
                    "Telemetry asynchronous apply complete sync_id=%s status=%s",
                    sync_id,
                    result["status"],
                )
                return result
            if result["failed"]:
                raise AsyncApplyError(
                    f"Telemetry asynchronous apply failed for operation {sync_id}"
                )
            logger.info(
                "Telemetry asynchronous apply pending sync_id=%s poll=%d/%d",
                sync_id,
                poll_number,
                poll_count,
            )
    raise AsyncApplyError(
        f"Telemetry asynchronous apply exceeded {ASYNC_APPLY_TIMEOUT_SECONDS} seconds "
        f"for operation {sync_id}"
    )
