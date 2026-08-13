import argparse
import sys

from shared.logging_adapter import configure_logging

from .scraper import SothebysScraper


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Sotheby's lots to Postgres.")
    parser.add_argument(
        "--skip", type=int, default=0, help="Number of lot IDs to skip."
    )
    parser.add_argument(
        "--max-calendar-pages",
        type=int,
        default=None,
        help="Limit number of calendar pages to scan (default: no limit).",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Delete existing Sotheby's lots before scraping.",
    )
    parser.add_argument(
        "--min-wait", type=float, default=0.25, help="Minimum delay between requests."
    )
    parser.add_argument(
        "--max-wait", type=float, default=0.75, help="Maximum delay between requests."
    )
    parser.add_argument(
        "--country",
        type=str,
        default="DE",
        help="GraphQL countryOfOrigin parameter (default: DE).",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="ENGLISH",
        help="GraphQL translation language (default: ENGLISH).",
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
        "--max-lots-per-auction",
        type=int,
        default=None,
        help="Only process up to this many lots per auction (default: no limit).",
    )
    return parser.parse_args()


def main() -> None:
    configure_logging()
    args = parse_args()
    scraper = SothebysScraper(
        max_calendar_pages=args.max_calendar_pages,
        min_wait=args.min_wait,
        max_wait=args.max_wait,
        purge=args.purge,
        images_dir=args.images_dir,
        country=args.country,
        language=args.language,
        commit_every=args.commit_every,
        max_lots_per_auction=args.max_lots_per_auction,
    )
    scraper.run(skip=args.skip)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted by user.")
