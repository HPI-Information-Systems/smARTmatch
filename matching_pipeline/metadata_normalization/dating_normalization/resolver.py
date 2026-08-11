"""
Deterministic resolver: turns a list of schema.Mention objects (what the LLM
is asked to produce - see structured_prompt.py) into the final
{"start": int|None, "end": int|None} interval.
"""

from pathlib import Path
from typing import Optional

from matching_pipeline.metadata_normalization.dating_normalization.schema import Mention

CURRENT_YEAR = 2026

Interval = tuple[Optional[int], Optional[int]]


def century_bounds(n: int, sub: Optional[str]) -> Interval:
    # S = start of century n (e.g. n=19 -> S=1800)
    s = (n - 1) * 100
    e = n * 100
    sub = sub or "full"
    if sub == "full":
        start, end = s, e
    elif sub in ("early", "q1"):
        start, end = s, s + 25
    elif sub == "q2":
        start, end = s + 25, s + 50
    elif sub == "mid":
        start, end = s + 25, s + 75
    elif sub == "first_half":
        start, end = s, s + 50
    elif sub == "q3":
        start, end = s + 50, s + 75
    elif sub == "second_half":
        start, end = s + 50, e
    elif sub in ("late", "q4"):
        start, end = s + 75, e
    else:
        raise ValueError(f"unknown century subdivision: {sub!r}")
    return start, min(end, CURRENT_YEAR) # cap the end at CURRENT_YEAR

# catch decades mislabeled as kind="century"/"century_span"
def _looks_like_decade_number(n: Optional[int]) -> bool:
    return n is not None and n >= 100


def _century_or_decade_bounds(n: int, sub: Optional[str]) -> Interval:
    if _looks_like_decade_number(n):
        return decade_bounds(n, sub)
    return century_bounds(n, sub)


def decade_bounds(start_year: int, sub: Optional[str]) -> Interval:
    """early=offset 0-5, mid=offset 3-7, late=offset 5-10."""
    sub = sub or "full"
    if sub == "full":
        s, e = start_year, start_year + 10
    elif sub == "early":
        s, e = start_year, start_year + 5
    elif sub == "mid":
        s, e = start_year + 3, start_year + 7
    elif sub == "late":
        s, e = start_year + 5, start_year + 10
    else:
        raise ValueError(f"unknown decade subdivision: {sub!r}")
    return s, min(e, CURRENT_YEAR)


def _resolve_fragment(digits: int, birth_year: Optional[int]) -> Interval:
    # e.g. "in '42" -> reconstruct full year near birth_year.
    if birth_year is None or digits is None:
        return None, None
    digits = abs(digits)
    n_digits = max(len(str(digits)), 2)
    modulus = 10 ** n_digits
    base = (birth_year // modulus) * modulus
    candidate = base + digits
    if candidate < birth_year:
        candidate += modulus
    return candidate, candidate


def resolve_mention(m: Mention, birth_year: Optional[int] = None) -> Interval:
    if m.kind == "none":
        return None, None

    if m.kind == "year":
        if m.n1 is None or not (100 <= m.n1 <= CURRENT_YEAR):
            return None, None
        if m.hedge:
            start, end = m.n1 - 5, m.n1 + 5
        else:
            start, end = m.n1, m.n1

    elif m.kind == "fragment":
        start, end = _resolve_fragment(m.n1, birth_year)

    elif m.kind == "century":
        start, end = _century_or_decade_bounds(m.n1, m.subdivision)

    elif m.kind == "century_span":
        start, _ = _century_or_decade_bounds(m.n1, m.subdivision)
        _, end = _century_or_decade_bounds(m.n2, m.subdivision2)

    elif m.kind == "decade":
        start, end = decade_bounds(m.n1, m.subdivision)

    elif m.kind == "decade_span":
        start, _ = decade_bounds(m.n1, m.subdivision)
        _, end = decade_bounds(m.n2, m.subdivision2)

    elif m.kind == "range":
        if m.hedge:
            start, end = m.n1 - 5, m.n2 + 5
        else:
            start, end = m.n1, m.n2

    elif m.kind == "open_after":
        start, end = m.n1, None

    elif m.kind == "open_before":
        start, end = None, m.n1

    else:
        raise ValueError(f"unknown mention kind: {m.kind!r}")

    if start is not None and end is not None and start > end:
        return None, None

    if m.truncate_start:
        start = None
    if m.truncate_end:
        end = None
    return start, end


def combine_intervals(intervals: list[Interval]) -> Interval:
    if not intervals:
        return None, None

    starts = [s for s, _e in intervals]
    ends = [e for _s, e in intervals]
    overall_start = None if any(s is None for s in starts) else min(starts)
    overall_end = None if any(e is None for e in ends) else max(ends)
    return overall_start, overall_end


def resolve_dating(mentions: list[Mention], birth_year: Optional[int] = None) -> Interval:
    if not mentions:
        return None, None
    intervals = []
    for m in mentions:
        try:
            iv = resolve_mention(m, birth_year)
        except (ValueError, TypeError):
            iv = (None, None)
        intervals.append(iv)
    informative = [iv for iv in intervals if iv != (None, None)]  # drop unresolved mentions
    if not informative:
        return None, None
    return combine_intervals(informative)
