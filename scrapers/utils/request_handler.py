import random
import time

import requests
from requests import Response

from shared.logging_adapter import get_logger

from .user_agents import VERIFIED_USER_AGENTS, choose_user_agent

logger = get_logger(__name__)


def _default_log(message: str) -> None:
    log = logger.error if "[fail]" in message.lower() else logger.info
    log("%s", message)


# Backwards-compatible public alias used by older call sites.
USER_AGENTS = VERIFIED_USER_AGENTS


def generate_headers() -> dict[str, str]:
    headers = {
        "User-Agent": choose_user_agent(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    return headers


def handle_request(
    url,
    max_retries=5,
    min_wait=0.25,
    max_wait=0.25,
    *,
    log=_default_log,
    session: requests.Session | None = None,
    headers: dict[str, str] | None = None,
    expected_status: int | None = None,
) -> Response | None:
    """Retry-wrapped HTTP GET.

    ``log`` is a callable that prints one line.  Pass the owning scraper's
    bound ``self.log`` so retry messages share the ``[<id>]`` prefix.
    """
    attempts = 0
    success = False

    while not success and attempts < max_retries:
        time.sleep(
            random.uniform(min_wait, max_wait)
        )  # circumvent rate limit / upper bound for network requests
        try:
            requester = session or requests
            request_headers = headers if headers is not None else generate_headers()
            r = requester.get(url, headers=request_headers, timeout=30)
            r.raise_for_status()
            if expected_status is not None and r.status_code != expected_status:
                raise requests.HTTPError(
                    f"Expected HTTP {expected_status}, received HTTP {r.status_code}",
                    response=r,
                )
            return r
        except requests.RequestException as error:
            attempts += 1
            log(f"[fail] {url}: {error}")
            log(f"[retry] attempt {attempts}/{max_retries}")

    return None


def request_html(
    url,
    max_retries=5,
    min_wait=0.25,
    max_wait=0.25,
    *,
    log=_default_log,
    session: requests.Session | None = None,
    headers: dict[str, str] | None = None,
    expected_status: int | None = None,
):
    r = handle_request(
        url,
        max_retries,
        min_wait,
        max_wait,
        log=log,
        session=session,
        headers=headers,
        expected_status=expected_status,
    )
    if r is None:
        return None

    try:
        encoding = (r.encoding or "").lower()
        if not encoding or encoding == "iso-8859-1":
            apparent = r.apparent_encoding
            if apparent:
                r.encoding = apparent
        return r.text
    except Exception:
        log("[fail] could not request HTML")
        return None


def request_image(
    url, max_retries=5, min_wait=0.25, max_wait=0.25, *, log=_default_log
):
    r = handle_request(url, max_retries, min_wait, max_wait, log=log)
    if r is None:
        return None

    try:
        return r.content
    except Exception:
        log("[fail] could not request image")
        return None
