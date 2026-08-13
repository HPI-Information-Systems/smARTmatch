import random
import time

import requests
from requests import Response

from shared.logging_adapter import get_logger

logger = get_logger(__name__)


def _default_log(message: str) -> None:
    log = logger.error if "[fail]" in message.lower() else logger.info
    log("%s", message)


USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.10 Safari/605.1.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.3",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.3",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.3",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Trailer/93.3.8652.5",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 OPR/117.0.0.",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/70.0.3538.102 Safari/537.36 Edge/18.1958",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.3",
]


def generate_headers() -> dict[str, str]:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    return headers


def handle_request(
    url, max_retries=5, min_wait=0.25, max_wait=0.25, *, log=_default_log
) -> Response:
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
            r = requests.get(url, headers=generate_headers(), timeout=30)
            r.raise_for_status()
            return r
        except requests.RequestException as error:
            attempts += 1
            log(f"[fail] {url}: {error}")
            log(f"[retry] attempt {attempts}/{max_retries}")


def request_html(
    url, max_retries=5, min_wait=0.25, max_wait=0.25, *, log=_default_log
):
    r = handle_request(url, max_retries, min_wait, max_wait, log=log)
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
    try:
        return r.content
    except Exception:
        log("[fail] could not request image")
        return None
