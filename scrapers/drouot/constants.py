from __future__ import annotations

import re

PARSER = "lxml"
BASE_URL = "https://drouot.com"
DEFAULT_CATEGORY_URL = "https://drouot.com/de/c/626/gemalde"

LOT_PATH_RE = re.compile(r"^/[a-z]{2}/l/\d+-")
LOT_ID_RE = re.compile(r"/l/(\d+)-")
LOT_NUMBER_PREFIX_RE = re.compile(
    r"^\s*(?:\d+[A-Za-z]?(?:[-/]\d+[A-Za-z]?){0,4}(?:\s*(?:bis|ter))?)\s*[-–—]\s*",
    re.IGNORECASE,
)

TITLE_SECTION_SPLIT_RE = re.compile(r"\s+[–—-]\s+|\s+\|\s+")
