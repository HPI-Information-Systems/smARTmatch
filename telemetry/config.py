"""Environment loading and endpoint validation for telemetry."""

import ipaddress
import math
import socket
from urllib.parse import urlsplit

from matching_pipeline.shared.env import (
    env_bool,
    env_image_root,
    env_required_str,
    env_str,
)
from telemetry.constants import (
    DEFAULT_PAGE_DELAY_MAX_SECONDS,
    DEFAULT_PAGE_DELAY_MIN_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    REMOTE_TELEMETRY_HOST,
)
from telemetry.models import TelemetrySettings


def _is_local_telemetry_host(hostname: str) -> bool:
    hostname = hostname.rstrip(".").lower()
    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname == "host.docker.internal"
        or "." not in hostname
    ):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    hostname,
                    None,
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror:
            return False
    else:
        addresses = {str(address)}
    return bool(addresses) and all(
        (
            ipaddress.ip_address(address).is_private
            or ipaddress.ip_address(address).is_loopback
            or ipaddress.ip_address(address).is_link_local
        )
        for address in addresses
    )


def _nonnegative_float_setting(name: str, default: float) -> float:
    value_text = env_str(name, str(default))
    assert value_text is not None
    try:
        value = float(value_text)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be greater than or equal to zero")
    return value


def load_telemetry_settings() -> TelemetrySettings | None:
    """Return validated settings, or ``None`` when telemetry is disabled."""
    if not _telemetry_enabled():
        return None

    endpoint = env_required_str("TELEMETRY_ENDPOINT")
    parsed = urlsplit(endpoint)
    scheme = parsed.scheme.lower()
    if not parsed.hostname:
        raise ValueError("TELEMETRY_ENDPOINT must be an absolute URL")
    insecure_local_http = env_bool("TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP")
    local_endpoint_allowed = insecure_local_http and _is_local_telemetry_host(
        parsed.hostname
    )
    if scheme != "https" and not (scheme == "http" and local_endpoint_allowed):
        raise ValueError(
            "TELEMETRY_ENDPOINT must use HTTPS; insecure HTTP is allowed only "
            "for local hosts when TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP=true"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("TELEMETRY_ENDPOINT must not contain URL credentials")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname != REMOTE_TELEMETRY_HOST and not local_endpoint_allowed:
        raise ValueError(
            "Remote TELEMETRY_ENDPOINT host must be " f"{REMOTE_TELEMETRY_HOST}"
        )
    if parsed.fragment:
        raise ValueError("TELEMETRY_ENDPOINT must not contain a fragment")
    auth_token = env_required_str("TELEMETRY_AUTH_TOKEN")

    timeout_text = env_str("TELEMETRY_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    assert timeout_text is not None
    try:
        timeout_seconds = float(timeout_text)
    except ValueError as exc:
        raise ValueError("TELEMETRY_TIMEOUT_SECONDS must be a number") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("TELEMETRY_TIMEOUT_SECONDS must be greater than zero")
    page_delay_min_seconds = _nonnegative_float_setting(
        "TELEMETRY_PAGE_DELAY_MIN_SECONDS", DEFAULT_PAGE_DELAY_MIN_SECONDS
    )
    page_delay_max_seconds = _nonnegative_float_setting(
        "TELEMETRY_PAGE_DELAY_MAX_SECONDS", DEFAULT_PAGE_DELAY_MAX_SECONDS
    )
    if page_delay_max_seconds < page_delay_min_seconds:
        raise ValueError(
            "TELEMETRY_PAGE_DELAY_MAX_SECONDS must be greater than or equal to "
            "TELEMETRY_PAGE_DELAY_MIN_SECONDS"
        )

    return TelemetrySettings(
        endpoint=endpoint,
        auth_token=auth_token,
        image_root=env_image_root(),
        timeout_seconds=timeout_seconds,
        match_expiration_seconds=_duration_seconds(
            env_str("SMARTMATCH_MATCH_EXPIRATION_AGE", "30d") or "30d"
        ),
        page_delay_min_seconds=page_delay_min_seconds,
        page_delay_max_seconds=page_delay_max_seconds,
    )


def _telemetry_enabled() -> bool:
    raw_value = env_str("TELEMETRY_ENABLED")
    if raw_value is None:
        return env_bool("TELEMETRY_ENABLED")
    normalized = raw_value.lower()
    if normalized not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ValueError(
            "Environment variable TELEMETRY_ENABLED must be a boolean value"
        )
    return env_bool("TELEMETRY_ENABLED")


def _duration_seconds(value: str) -> int:
    text = value.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(text) < 2 or text[-1] not in units:
        raise ValueError(
            "SMARTMATCH_MATCH_EXPIRATION_AGE must be a positive duration such as 30d"
        )
    try:
        amount = float(text[:-1])
    except ValueError as exc:
        raise ValueError(
            "SMARTMATCH_MATCH_EXPIRATION_AGE must be a positive duration such as 30d"
        ) from exc
    seconds = amount * units[text[-1]]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("SMARTMATCH_MATCH_EXPIRATION_AGE must be greater than zero")
    return int(seconds)
