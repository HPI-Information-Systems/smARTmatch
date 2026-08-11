import argparse
import sys

from .scraper import LostArtScraper


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Lost Art records to Postgres.")
    parser.add_argument(
        "--skip", type=int, default=0, help="Number of index rows to skip."
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Limit number of records to process (default: all).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit number of search result pages to fetch (default: no limit).",
    )
    parser.add_argument(
        "--page-delay",
        type=float,
        default=0.0,
        help="Delay between search page requests in seconds.",
    )
    parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="Start offset for the Lost Art search results (default: 0).",
    )
    parser.add_argument(
        "--search-url",
        type=str,
        default=None,
        help=(
            "Optional custom Lost Art search URL to scrape exactly that results page "
            "(disables normal pagination)."
        ),
    )
    parser.add_argument(
        "--details-sleep",
        type=float,
        default=0.5,
        help="Delay between detail page requests in seconds.",
    )
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Do not download gallery images.",
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
        help="Delete existing lost artwork rows before scraping.",
    )
    parser.add_argument(
        "--anubis-cookie-verification",
        type=str,
        default=None,
        help="Value of techaro.lol-anubis-cookie-verification cookie.",
    )
    parser.add_argument(
        "--anubis-auth",
        type=str,
        default=None,
        help="Value of techaro.lol-anubis-auth cookie.",
    )
    parser.add_argument(
        "--cookie-header",
        type=str,
        default=None,
        help="Optional raw Cookie header string (alternative to individual cookie flags).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scraper = LostArtScraper(
        max_pages=args.max_pages,
        page_delay=args.page_delay,
        start_offset=args.start_offset,
        search_url=args.search_url,
        details_sleep=args.details_sleep,
        download_images=not args.skip_images,
        images_dir=args.images_dir,
        max_rows=args.max_rows,
        commit_every=args.commit_every,
        purge=args.purge,
        anubis_cookie_verification=args.anubis_cookie_verification,
        anubis_auth=args.anubis_auth,
        cookie_header=args.cookie_header,
    )
    scraper.run(skip=args.skip)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted by user.")
