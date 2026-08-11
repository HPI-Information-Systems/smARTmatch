import argparse
import sys

from .scraper import ChristiesScraper


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Christie's lots to Postgres.")
    parser.add_argument(
        "--skip", type=int, default=0, help="Number of lot IDs to skip."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit number of search pages to scan (default: no limit).",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Limit number of lots to scrape (default: all).",
    )
    parser.add_argument(
        "--min-wait",
        type=float,
        default=0.25,
        help="Minimum delay between requests in seconds (default: 0.25).",
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=0.75,
        help="Maximum delay between requests in seconds (default: 0.75).",
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
        help="Directory to store downloaded images (defaults to db/data-production/images).",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=50,
        help="Commit after this many records (default: 50).",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Delete existing Christie's lots before scraping.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scraper = ChristiesScraper(
        max_pages=args.max_pages,
        max_rows=args.max_rows,
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
