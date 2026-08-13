from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from shared.logging_adapter import get_logger

try:
    from .generate_user_agent import generate_user_agent
except ImportError:  # pragma: no cover - fallback for direct script usage
    from generate_user_agent import generate_user_agent


logger = get_logger(__name__)


def _log(message: str) -> None:
    """Log with the same platform tag used by ``LostArtScraper``."""
    log = logger.error if "[fail]" in message.lower() else logger.info
    log("[los] %s", message)


DATA_DIR = Path(__file__).resolve().parent / "data"
INDEX_FILE = DATA_DIR / "index.csv"
OUTPUT_FILE = DATA_DIR / "page_details.csv"
READ_MORE_SELECTOR = ".text-read-more__button"

SESSION = requests.Session()
_USER_AGENT = generate_user_agent()
SESSION.headers.update(
    {
        "User-Agent": _USER_AGENT.ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
)


def iter_index_rows(start_row: int = 0) -> Iterable[tuple[int, dict[str, str]]]:
    if not INDEX_FILE.exists():
        raise FileNotFoundError(f"Index file not found: {INDEX_FILE}")

    with INDEX_FILE.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row_number, row in enumerate(reader):
            if row_number < start_row:
                continue
            yield row_number, row


def fetch_detail_page(url: str, attempts: int = 3, delay: float = 1.5) -> Optional[str]:
    for attempt in range(1, attempts + 1):
        try:
            response = SESSION.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            _log(f"[fail] fetch {url} (attempt {attempt}/{attempts}): {exc}")
            if attempt == attempts:
                return None
            time.sleep(delay)
    return None


def normalize_label(text: str) -> str:
    cleaned = " ".join(text.split()).strip()
    if cleaned.endswith(":"):
        cleaned = cleaned[:-1]
    return cleaned


def strip_read_more_controls(node: Tag) -> None:
    for control in node.select(READ_MORE_SELECTOR):
        control.decompose()


def extract_item_text(item: Tag) -> str:
    strip_read_more_controls(item)
    parts: list[str] = []

    def visit(node) -> None:
        if isinstance(node, NavigableString):
            chunk = str(node).strip()
            if chunk:
                parts.append(chunk)
            return

        if not isinstance(node, Tag):
            return

        classes = node.get("class") or []
        if "contact-box" in classes:
            text = node.get_text("\n", strip=True)
            if text:
                normalized_lines = [
                    line.strip() for line in text.splitlines() if line.strip()
                ]
                parts.append("; ".join(normalized_lines))
            return
        if node is not item and "field" in classes:
            return
        if "text-read-more__button" in classes:
            return

        for child in node.children:
            visit(child)

    visit(item)

    if parts:
        return " ".join(parts)

    return ""


def extract_field_data(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    root = (
        soup.select_one(".page-body .page-content__content")
        or soup.select_one(".page-body")
        or soup.select_one(".page-content__content")
        or soup
    )

    collected: dict[str, list[str]] = {}
    field_nodes = root.select("div.field")
    if not field_nodes and root is not soup:
        field_nodes = soup.select(
            ".page-body div.field, .page-content__content div.field"
        )

    def is_inside_contact_box(node: Tag) -> bool:
        return node.find_parent(class_="contact-box") is not None

    filtered_fields: list[Tag] = [
        field for field in field_nodes if not is_inside_contact_box(field)
    ]

    for field in filtered_fields:
        label_el = field.find("div", class_="field__label", recursive=False)
        if label_el is None:
            continue

        label = normalize_label(label_el.get_text(" ", strip=True))
        if not label:
            continue

        item_nodes = field.find_all("div", class_="field__item", recursive=False)
        values = [extract_item_text(node) for node in item_nodes]
        values = [value for value in values if value]
        if not values:
            continue

        collected.setdefault(label, []).extend(values)

    return {label: "; ".join(values) for label, values in collected.items()}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Lost Art object detail pages and export merged data as CSV."
    )
    parser.add_argument(
        "--start-row",
        type=int,
        default=0,
        help="Row number (0-based) in index.csv to start from.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Maximum number of index rows to process (default: all).",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help="Seconds to sleep between requests to avoid hammering the site.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_FILE,
        help="Target CSV file for extracted data (default: data/page_details.csv).",
    )
    return parser.parse_args(argv)


def scrape_detail_pages(
    *,
    start_row: int = 0,
    max_rows: Optional[int] = None,
    sleep: float = 0.5,
    output_path: Path = OUTPUT_FILE,
    verbose: bool = True,
) -> int:
    """Scrape Lost Art detail pages using the prepared index."""

    start_row = max(start_row, 0)
    max_rows = max_rows if max_rows is None else max(max_rows, 0)

    records: list[dict[str, str]] = []
    columns: list[str] = ["Row", "Lost Art ID", "Link"]
    column_set = set(columns)

    processed = 0
    for row_number, row in iter_index_rows(start_row):
        if max_rows is not None and processed >= max_rows:
            break

        link = (row.get("Link") or row.get("link") or "").strip()
        lost_art_id = (row.get("Lost Art ID") or row.get("lost art id") or "").strip()

        if not link:
            if verbose:
                _log(f"[skip] row {row_number}: missing Link")
            continue

        html = fetch_detail_page(link)
        if html is None:
            if verbose:
                _log(f"[fail] row {row_number}: giving up on {link}")
            continue

        field_data = extract_field_data(html)
        record: dict[str, str] = {
            "Row": str(row_number),
            "Lost Art ID": lost_art_id,
            "Link": link,
        }

        for key, value in field_data.items():
            if key not in column_set:
                columns.append(key)
                column_set.add(key)
            record[key] = value

        records.append(record)
        processed += 1
        if verbose:
            _log(f"[capture] row {row_number}: {len(field_data)} fields")
        time.sleep(max(sleep, 0.0))

    if not records:
        if verbose:
            _log("[done] no records captured")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for record in records:
            writer.writerow({column: record.get(column, "") for column in columns})

    if verbose:
        _log(f"[write] {len(records)} records with {len(columns)} columns to {output_path}")

    return len(records)


def run_cli(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)
    try:
        scrape_detail_pages(
            start_row=args.start_row,
            max_rows=args.max_rows,
            sleep=args.sleep,
            output_path=args.output,
        )
    except KeyboardInterrupt:
        sys.exit("Interrupted by user.")


__all__ = [
    "DATA_DIR",
    "INDEX_FILE",
    "OUTPUT_FILE",
    "fetch_detail_page",
    "iter_index_rows",
    "run_cli",
    "scrape_detail_pages",
]
