from __future__ import annotations

import re

PARSER = "lxml"
ROOT_URL = "https://www.lot-tissimo.com"
BASE_URL = f"{ROOT_URL}/de-de/kaufen/kunst/?sortterm=publishedDate"
GEMAELDE_URL = f"{ROOT_URL}/de-de/kaufen/kunst/gemaelde-und-mischtechniken?sortterm=publishedDate"

PAGE_COUNT_RE = re.compile(r'data-pages="(\d+)"')
LOT_LINK_RE = re.compile(
    r"/de-de/auction-catalogues/(?:\w|-|/)+/catalogue-id-(?:\w|-)+/lot-(?:\w|-)+"
)
LOT_ID_RE = re.compile(r"lot-([\w-]+)")
LOT_NUMBER_JSON_RE = re.compile(r'"lotNumber"\s*:\s*"([^"]+)"')
LOT_START_DATE_RE = re.compile(r'"lotStartDate"\s*:\s*"([^"]+)"')
LOT_END_DATE_RE = re.compile(r'"lotEndDate"\s*:\s*"([^"]+)"')
AUCTION_CITY_RE = re.compile(r'"auctionCity"\s*:\s*"([^"]+)"')
AUCTION_COUNTRY_RE = re.compile(r'"auctionCountry"\s*:\s*"([^"]+)"')
IMAGE_RE = re.compile(r"https://[^\"'\s>]+\.(?:jpe?g|png|webp)(?:\?[^\"'\s>]*)?", flags=re.IGNORECASE)
AUCTIONEER_URL_RE = re.compile(r"^(https?://www\.lot-tissimo\.com/de-de/auction-catalogues/[^/]+)")
DATA_LAYER_PUSH_MARKER = "window.dataLayer.push("

