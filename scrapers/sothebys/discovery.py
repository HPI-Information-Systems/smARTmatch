from __future__ import annotations

import base64
import binascii
import json
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from uuid import UUID

from .constants import HREF_PATTERN, NEXT_DATA_PATTERN


def calendar_page_url(base: str, page_number: int) -> str:
    split = urlsplit(base)
    pairs = [
        (key, value)
        for (key, value) in parse_qsl(split.query, keep_blank_values=True)
        if key not in {"p", "_requestType"}
    ]
    pairs.append(("p", str(page_number)))
    pairs.append(("_requestType", "ajax"))

    new_query = urlencode(pairs, doseq=True)
    return urlunsplit((split.scheme, split.netloc, split.path, new_query, split.fragment))


def extract_buy_links(html: str, *, base_url: str) -> list[str]:
    links: list[str] = []
    seen_on_page: set[str] = set()

    for href in HREF_PATTERN.findall(html):
        trimmed = href.strip()
        if "/buy/" not in trimmed:
            continue

        absolute = urljoin(base_url, trimmed)
        if absolute in seen_on_page:
            continue

        seen_on_page.add(absolute)
        links.append(absolute)

    return links


def _normalize_auction_id(value: str) -> str | None:
    """Return the UUID expected by Sotheby's API from raw or Relay IDs."""

    candidates = [value]
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        if decoded.startswith("Auction_"):
            candidates.append(decoded.removeprefix("Auction_"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        pass

    for candidate in candidates:
        try:
            return str(UUID(candidate))
        except (AttributeError, ValueError):
            continue
    return None


def extract_auction_id(html: str) -> str:
    """Pull the API auction UUID out of the page's ``__NEXT_DATA__`` Apollo cache."""

    match = NEXT_DATA_PATTERN.search(html)
    if not match:
        raise ValueError("Unable to locate __NEXT_DATA__ script in the page source.")

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"__NEXT_DATA__ payload is not valid JSON: {exc}") from exc

    cache = (
        data.get("props", {})
        .get("pageProps", {})
        .get("apolloCache", {})
    )
    if not isinstance(cache, dict):
        raise ValueError("__NEXT_DATA__ apolloCache is missing or malformed.")

    for key in cache:
        if not isinstance(key, str) or not key.startswith("Auction:"):
            continue
        auction_id = _normalize_auction_id(key.split(":", 1)[1])
        if auction_id is not None:
            return auction_id

    raise ValueError("No usable Auction UUID found inside the __NEXT_DATA__ apollo cache.")
