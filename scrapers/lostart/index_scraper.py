from __future__ import annotations

import csv
import io
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from shared.logging_adapter import get_logger

try:
    from .generate_user_agent import generate_random_user_agent
except ImportError:  # pragma: no cover - fallback for direct script usage
    from generate_user_agent import generate_random_user_agent


logger = get_logger(__name__)


def _log(message: str) -> None:
    """Log with the same platform tag used by ``LostArtScraper``."""
    logger.info("[los] %s", message)


BASE_URL = "https://www.lostart.de/de/search-export/csv"
PAGE_SIZE = 500
DATA_DIR = Path(__file__).resolve().parent / "data"
INDEX_FILE = DATA_DIR / "index.csv"
USER_AGENT = generate_random_user_agent()


@dataclass
class PageResult:
    start: int
    rows: list[dict[str, str]]
    header: list[str]
    raw_text: str


def _build_url(start: int) -> str:
    params = {
        "start": start,
        "filter[type][0]": "Objektdaten",
        "filter[report_type][0]": "Suchmeldung",
    }
    return f"{BASE_URL}?{urlencode(params)}"


def fetch_page(start: int, *, retry: int = 3, delay: float = 1.0) -> PageResult:
    url = _build_url(start)
    attempt = 0

    while True:
        attempt += 1
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request) as response:
                raw = response.read()
        except (HTTPError, URLError) as exc:
            if attempt > retry:
                raise RuntimeError(f"Failed to fetch {url}") from exc
            time.sleep(delay)
            continue

        text = raw.decode("utf-8-sig")
        stream = io.StringIO(text)
        try:
            sample = stream.read(4096)
            stream.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=";,")
            reader = csv.DictReader(stream, dialect=dialect)
        except csv.Error:
            stream.seek(0)
            reader = csv.DictReader(stream, delimiter=";")

        raw_rows = list(reader)
        header = [name for name in (reader.fieldnames or []) if name]
        rows: list[dict[str, str]] = []
        for raw_row in raw_rows:
            cleaned = {key: value for key, value in raw_row.items() if key}
            rows.append(cleaned)
        return PageResult(start=start, rows=rows, header=header, raw_text=text)


def append_to_index(page: PageResult, *, first_page: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lines = page.raw_text.splitlines(keepends=True)
    if not lines:
        return
    content_lines = lines if first_page else lines[1:]
    if not content_lines:
        return
    mode = "w" if first_page else "a"
    with INDEX_FILE.open(mode, encoding="utf-8") as fh:
        fh.write("".join(content_lines))


def scrape_index(
    *,
    start_offset: int = 0,
    page_limit: Optional[int] = None,
    delay: float = 0.0,
    verbose: bool = True,
) -> int:
    """Download the Lost Art CSV index and store it locally.

    Args:
        start_offset: start value for pagination (usually 0).
        page_limit: optional maximum number of pages to fetch.
        delay: extra sleep (seconds) between requests.
        verbose: emit progress information to stdout.

    Returns:
        Total number of rows written to the index file.
    """

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    start = max(start_offset, 0)
    total_rows = 0
    pages_fetched = 0

    while True:
        page = fetch_page(start)
        if not page.rows:
            break

        append_to_index(page, first_page=(start == start_offset))

        total_rows += len(page.rows)
        pages_fetched += 1
        if verbose:
            _log(f"[page start={start}] rows={len(page.rows)} total_rows={total_rows}")

        start += PAGE_SIZE
        if page_limit is not None and pages_fetched >= page_limit:
            break

        if delay > 0:
            time.sleep(delay)

    if verbose:
        _log(f"[done] index scrape wrote {total_rows} rows")

    return total_rows


def run_cli() -> None:
    try:
        scrape_index()
    except KeyboardInterrupt:
        sys.exit("Interrupted by user.")


__all__ = [
    "DATA_DIR",
    "INDEX_FILE",
    "PageResult",
    "append_to_index",
    "fetch_page",
    "run_cli",
    "scrape_index",
]
