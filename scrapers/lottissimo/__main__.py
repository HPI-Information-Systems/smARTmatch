import argparse
import sys

from shared.logging_adapter import configure_logging

from .scraper import LottissimoScraper


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape lot-tissimo lots to Postgres.")
    parser.add_argument(
        "--skip", type=int, default=0, help="Number of lot links to skip."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit number of list pages to scan.",
    )
    parser.add_argument(
        "--max-lots",
        type=int,
        default=None,
        help="Limit number of lots to scrape after applying --skip.",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Delete existing lot-tissimo lots before scraping.",
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
        "--images-dir",
        type=str,
        default=None,
        help="Directory to store downloaded lot images (defaults to db/data-production/images).",
    )
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Do not download lot images.",
    )
    parser.add_argument(
        "--gemaelde-only",
        action="store_true",
        help="Scrape only the 'Gemaelde und Mischtechnike' category.",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=20,
        help="Commit after this many scraped lots (default: 20).",
    )
    return parser.parse_args()


def main() -> None:
    configure_logging()
    args = parse_args()
    lottissimo_scraper = LottissimoScraper(
        max_pages=args.max_pages,
        max_lots=args.max_lots,
        min_wait=args.min_wait,
        max_wait=args.max_wait,
        purge=args.purge,
        images_dir=args.images_dir,
        gemaelde_only=args.gemaelde_only,
        download_images=not args.skip_images,
        commit_every=args.commit_every,
    )
    lottissimo_scraper.run(skip=args.skip)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted by user.")
