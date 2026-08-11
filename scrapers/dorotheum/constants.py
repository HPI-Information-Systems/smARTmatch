from __future__ import annotations

import re

PARSER = "lxml"
ROOT_URL = "https://www.dorotheum.com"
DEFAULT_CATEGORY_URL = f"{ROOT_URL}/de/co/gemaelde-1/"

DEFAULT_ACCEPT_HEADER = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
DEFAULT_ACCEPT_LANGUAGE = "de-DE,de;q=0.9,en-US;q=0.7,en;q=0.5"
DEFAULT_BROWSER_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
)

LOT_PATH_RE = re.compile(r"^/[a-z]{2}/l/\d+/?$")
LOT_UID_RE = re.compile(r"/[a-z]{2}/l/(\d+)/?")

LOTS_SCRIPT_RE = re.compile(
    r"var\s+lots\s*=\s*(\{.*?\});\s*var\s+filialen\s*=\s*(\[.*?\]);",
    re.DOTALL,
)

LOT_NUMBER_RE = re.compile(r"Lot\s*Nr\.\s*([^\n]+)", re.IGNORECASE)
ESTIMATE_RANGE_RE = re.compile(r"(\d[\d.,]*)")
