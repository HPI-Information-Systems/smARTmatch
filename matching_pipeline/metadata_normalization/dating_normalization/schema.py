"""
Output schema for the LLM that is used by the resolver to compute the intervals.
A single dating input string can contain more than one mention (e.g.
"circa 1880 and 19th - early 20th century"), so the LLM's output for one
input item is a LIST[Mention]; resolver.combine_intervals() merges them.
"""

from dataclasses import dataclass
from typing import Literal, Optional

Kind = Literal[
    "year",         # bare or hedged single 4-digit year -> n1
    "fragment",     # short digit fragment needing artist context -> n1
    "century",      # n1 = century number (e.g. 19), subdivision = sub (see Subdivision Literal below)
    "century_span", # two centuries combined (cross-century, or "X./Y. Jh.")
    "decade",       # n1 = decade start year (e.g. 1920), subdivision = sub
    "decade_span",  # two decades combined ("1950er-1960er", "mid to late 1920s")
    "range",        # explicit yyyy-yyyy (or expanded yyyy-yy) range -> n1, n2
    "open_after",   # "nach/after/post X" -> n1, end=null
    "open_before",  # "vor/before X" -> start=null, n1
    "none",         # genuinely no date content -> null/null
]

Subdivision = Literal[
    "full", "early", "mid", "late", "first_half", "second_half", "q1", "q2", "q3", "q4",
]


@dataclass
class Mention:
    kind: Kind
    n1: Optional[int] = None
    n2: Optional[int] = None
    subdivision: Optional[Subdivision] = None
    subdivision2: Optional[Subdivision] = None
    hedge: bool = False
    truncate_start: bool = False
    truncate_end: bool = False

    @staticmethod
    def from_dict(d: dict) -> "Mention":
        known = {f for f in Mention.__dataclass_fields__}
        return Mention(**{k: v for k, v in d.items() if k in known})
