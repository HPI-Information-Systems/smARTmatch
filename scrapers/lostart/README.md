# Lost Art scrapers

Utilities for downloading data from lostart.de. Typical flow:

1. Run `scrape-index.py` to export search results into `data/index.csv`.
2. (Optional) Run `scrape-images.py` to grab gallery assets for each row.
3. Run `scrape-single-pages.py` to enrich each index row with the per-object detail fields.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r ../requirements.txt
```

## scrape-single-pages.py

Fetches every `Link` listed in `data/index.csv`, parses all labeled fields on the detail page, and writes them into a single CSV (default: `data/page_details.csv`). Columns are added dynamically, so attributes that appear only on some objects are still captured for future rows even if blank.

```bash
python scrape-single-pages.py --start-row 0 --max-rows 100 --sleep 0.75
```

Arguments:
- `--start-row`: first row in the index (0-based).
- `--max-rows`: cap number of rows to fetch (omit for all rows).
- `--sleep`: polite delay between requests in seconds (default 0.5).
- `--output`: destination CSV path.

The output always contains `Row`, `Lost Art ID`, and `Link`, followed by any additional attributes discovered in the processed pages.
