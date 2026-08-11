import argparse
import sys

from .constants import DEFAULT_CATEGORY_URL
from .scraper import DrouotScraper


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Drouot lots to Postgres.")
    parser.add_argument(
        "--skip", type=int, default=0, help="Number of lot URLs to skip."
    )
    parser.add_argument(
        "--category-url",
        type=str,
        default=DEFAULT_CATEGORY_URL,
        help="Drouot category/listing URL used to collect lot URLs.",
    )
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="First listing page to fetch (default: 1).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="How many listing pages to scan from start-page (default: no limit).",
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
        help="Delete existing Drouot lots before scraping.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scraper = DrouotScraper(
        category_url=args.category_url,
        start_page=args.start_page,
        max_pages=args.max_pages,
        min_wait=args.min_wait,
        max_wait=args.max_wait,
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
