"""
Regex-based fast path for parsing dating strings into (start, end) year
ranges, before falling back to the LLM normalizer for anything unmatched.
"""

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

RangeTuple = Tuple[Optional[int], Optional[int]]
CURRENT_YEAR = date.today().year

APPROX_YEAR_RE = re.compile(
    r"^\s*(?P<approx>(?:ca\.?|circa|c\.?|um|around|about|wohl\s+um|vers|env\.?|environ)\s+)?(?P<year>\d{4})(?:\s*\((?P<approx_suffix>ca\.?|circa|c\.?|um|around|about|wohl\s+um|vers|env\.?|environ)\))?\s*$",
    re.IGNORECASE,
)

UNCERTAIN_YEAR_RE = re.compile(r"^\s*(?P<year>\d{4})\s*\(\?\)\s*$")

BRACKETED_YEAR_RE = re.compile(r"^\s*\[(?P<prefix>\d{1,2})\](?P<suffix>\d{2})\s*$")
PARENTHESIZED_YEAR_RE = re.compile(r"^\s*\((?P<prefix>\d{1,2})\)(?P<suffix>\d{2})\s*$")
SHORT_YEAR_RE = re.compile(r"^\s*(?P<year>\d{2})\s*$")
FULL_INTERVAL_RE = re.compile(r"^\s*(?P<y1>\d{4})\s*[-/]\s*(?P<y2>\d{4})\s*$")
SHORT_SECOND_INTERVAL_RE = re.compile(r"^\s*(?P<y1>\d{4})\s*[-/]\s*(?P<y2>\d{2})\s*$")

DAT_PREFIX_RE = re.compile(
    r"^\s*(?:dat\.?|datiert|datiert\.|dated|datato|dat[ée]e?\.?)\s*(?P<year>\d{4})\s*$",
    re.IGNORECASE,
)

GERMAN_CENTURY_RE = re.compile(
    r"^\s*(?P<century>\d{1,2})\.?\s*(?:jahrhundert|jhdt\.?|jh\.?)(?:\s*)$",
    re.IGNORECASE,
)

ENGLISH_CENTURY_RE = re.compile(
    r"^\s*(?P<century>\d{1,2})(?:st|nd|rd|th)\s*(?:century|cent\.?|c\.?)\s*$",
    re.IGNORECASE,
)

FRENCH_CENTURY_RE = re.compile(
    r"^\s*(?P<century>\d{1,2})(?:er|e|ème|eme)?\s*(?:si[eè]cle(?:s)?|siècl\.|s\.)\.?\s*$",
    re.IGNORECASE,
)


def _parse_birth_year(value: str | None) -> int | None:
    if not value:
        return None
    m = re.match(r"\d{4}", str(value).strip())
    return int(m.group()) if m else None


def normalize_text(text: str) -> str:
    return text.strip().replace("–", "-").replace("—", "-")


def century_start(century: int) -> int:
    return (century - 1) * 100


def expand_short_year(full_year: int, short_year: int) -> int:
    base = (full_year // 100) * 100
    candidate = base + short_year
    if candidate < full_year:
        candidate += 100
    return candidate


def parse_dating_range(dating: str) -> tuple[RangeTuple, bool]:
    if not dating or not dating.strip():
        return (None, None), False

    text = normalize_text(dating)

    m = FULL_INTERVAL_RE.fullmatch(text)
    if m:
        return (int(m.group("y1")), int(m.group("y2"))), True

    m = SHORT_SECOND_INTERVAL_RE.fullmatch(text)
    if m:
        y1 = int(m.group("y1"))
        return (y1, expand_short_year(y1, int(m.group("y2")))), True

    m = DAT_PREFIX_RE.fullmatch(text)
    if m:
        year = int(m.group("year"))
        return (year, year), True

    m = BRACKETED_YEAR_RE.fullmatch(text)
    if m:
        year = int(f"{m.group('prefix')}{m.group('suffix')}")
        return (year, year), True

    m = PARENTHESIZED_YEAR_RE.fullmatch(text)
    if m:
        year = int(f"{m.group('prefix')}{m.group('suffix')}")
        return (year, year), True

    m = SHORT_YEAR_RE.fullmatch(text)
    if m:
        year = int(m.group("year"))
        return (year, year), True

    m = GERMAN_CENTURY_RE.fullmatch(text)
    if m:
        start = century_start(int(m.group("century")))
        return (start, start + 100), True

    m = ENGLISH_CENTURY_RE.fullmatch(text)
    if m:
        start = century_start(int(m.group("century")))
        return (start, start + 100), True

    m = FRENCH_CENTURY_RE.fullmatch(text)
    if m:
        start = century_start(int(m.group("century")))
        return (start, start + 100), True

    m = UNCERTAIN_YEAR_RE.fullmatch(text)
    if m:
        year = int(m.group("year"))
        return (year, year), True

    m = APPROX_YEAR_RE.fullmatch(text)
    if not m:
        return (None, None), False

    year = int(m.group("year"))
    if m.group("approx") or m.group("approx_suffix"):
        return (year - 5, year + 5), True
    return (year, year), True


def normalize_with_regex(descriptions_file: Path, unmatched_file: Path) -> None:
    records = []
    with open(descriptions_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    n_matched = n_unmatched = n_empty = 0

    with open(descriptions_file, "w", encoding="utf-8") as f_out, open(
        unmatched_file, "w", encoding="utf-8"
    ) as f_unmatched:
        for rec in records:
            dating = (rec.get("dating") or "").strip()
            if dating:
                (start, end), matched = parse_dating_range(dating)
                if matched and start is not None and start == end and 0 <= start <= 99:
                    birth_year = _parse_birth_year(rec.get("date_of_birth"))
                    if birth_year is not None:
                        expanded = expand_short_year(birth_year, start)
                        start = end = expanded
                    else:
                        matched = True
                        start = end = None
                if matched:
                    n_matched += 1
                else:
                    start, end = None, None
                    n_unmatched += 1
                    entry: dict = {"id": rec["id"], "dating": dating}
                    if rec.get("author"):
                        entry["author"] = rec["author"]
                    if rec.get("date_of_birth"):
                        entry["date_of_birth"] = rec["date_of_birth"]
                    if rec.get("date_of_death"):
                        entry["date_of_death"] = rec["date_of_death"]
                    f_unmatched.write(json.dumps(entry, ensure_ascii=False) + "\n")
            else:
                start, end = None, None
                n_empty += 1
            f_out.write(
                json.dumps(
                    {**rec, "dating_start": start, "dating_end": end},
                    ensure_ascii=False,
                )
                + "\n"
            )

    logger.info(f"Total:     {len(records)}")
    logger.info(f"Matched:   {n_matched}")
    logger.info(f"Unmatched: {n_unmatched}  → {unmatched_file.name}")
    logger.info(f"Empty:     {n_empty}")
