from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class UserAgent:
    ua: str
    sec_ch_ua: str | None = None
    sec_ch_platform: str | None = None
    sec_ch_mobile: str | None = None


_USER_AGENTS: List[tuple[str, float]] = [
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.10 Safari/605.1.1",
        43.03,
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.3",
        21.05,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.3",
        17.34,
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.3",
        3.72,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Trailer/93.3.8652.5",
        2.48,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.",
        2.48,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.",
        2.48,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 OPR/117.0.0.",
        2.48,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.",
        1.24,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/70.0.3538.102 Safari/537.36 Edge/18.1958",
        1.24,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.",
        1.24,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.3",
        1.24,
    ),
]

_WEIGHTS = [entry[1] for entry in _USER_AGENTS]


def generate_user_agent(rng: random.Random | None = None) -> UserAgent:
    if rng is None:
        rng = random
    ua_string, _ = rng.choices(_USER_AGENTS, weights=_WEIGHTS, k=1)[0]
    return UserAgent(ua=ua_string)


def generate_random_user_agent(rng: random.Random | None = None) -> str:
    return generate_user_agent(rng).ua
