import argparse
import sys

from .constants import DEFAULT_CATEGORY_URL
from .scraper import DorotheumScraper


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape Dorotheum Gemälde lots to Postgres."
    )
    parser.add_argument(
        "--skip", type=int, default=0, help="Number of lot URLs to skip."
    )
    parser.add_argument(
        "--category-url",
        type=str,
        default=DEFAULT_CATEGORY_URL,
        help="Dorotheum listing URL used to collect lot URLs.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum listing pages to probe with ?page=N (default: first page only).",
    )
    parser.add_argument(
        "--max-lots",
        type=int,
        default=None,
        help="Maximum number of lots to scrape (default: all discovered lots).",
    )
    parser.add_argument(
        "--min-wait",
        type=float,
        default=0.25,
        help="Minimum delay between GET requests.",
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=0.75,
        help="Maximum delay between GET requests.",
    )
    parser.add_argument(
        "--request-max-retries",
        type=int,
        default=5,
        help="Maximum retries for listing/detail requests (default: 5).",
    )
    parser.add_argument(
        "--cookie-header",
        type=str,
        default=None,
        help="Optional raw Cookie header string (or use DOROTHEUM_COOKIE_HEADER env var).",
    )
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Do not download lot images.",
    )
    parser.add_argument(
        "--images-dir",
        type=str,
        default=None,
        help="Directory to store downloaded lot images (defaults to db/data-production/images).",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=20,
        help="Commit after this many scraped lots (default: 20).",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Delete existing Dorotheum lots before scraping.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    scraper = DorotheumScraper(
        category_url=args.category_url,
        max_pages=args.max_pages,
        max_lots=args.max_lots,
        min_wait=args.min_wait,
        max_wait=args.max_wait,
        request_max_retries=args.request_max_retries,
        cookie_header=args.cookie_header,
        download_images=not args.skip_images,
        images_dir=args.images_dir,
        commit_every=args.commit_every,
        purge=args.purge,
    )
    scraper.run(skip=args.skip)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted by user.")
