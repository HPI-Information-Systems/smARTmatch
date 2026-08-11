from __future__ import annotations

import argparse
import sys
from pathlib import Path

from detail_scraper import OUTPUT_FILE as DEFAULT_DETAILS_OUTPUT
from detail_scraper import scrape_detail_pages
from image_scraper import scrape_images
from index_scraper import INDEX_FILE, scrape_index


def _log(message: str) -> None:
    """Standalone log helper sharing the [los] prefix with LostArtScraper."""
    print(f"[los] {message}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the full Lost Art scraping pipeline (index, details, images)."
    )

    parser.add_argument(
        "--skip-index",
        action="store_true",
        help="Do not refresh the index CSV before proceeding.",
    )
    parser.add_argument(
        "--skip-details",
        action="store_true",
        help="Skip scraping detail pages (saves time if you only need images).",
    )
    parser.add_argument(
        "--skip-images",
        action="store_true",
        help="Skip downloading gallery images.",
    )

    parser.add_argument(
        "--index-page-limit",
        type=int,
        default=None,
        help="Optional maximum number of index pages to fetch.",
    )
    parser.add_argument(
        "--index-delay",
        type=float,
        default=0.0,
        help="Seconds to sleep between index page requests (default: 0).",
    )

    parser.add_argument(
        "--details-start-row",
        type=int,
        default=0,
        help="Row number to start detail scraping from (0-based).",
    )
    parser.add_argument(
        "--details-max-rows",
        type=int,
        default=None,
        help="Maximum detail rows to process (default: all).",
    )
    parser.add_argument(
        "--details-sleep",
        type=float,
        default=0.5,
        help="Delay between detail page requests in seconds.",
    )
    parser.add_argument(
        "--details-output",
        type=Path,
        default=DEFAULT_DETAILS_OUTPUT,
        help=f"Destination CSV for detail fields (default: {DEFAULT_DETAILS_OUTPUT}).",
    )

    parser.add_argument(
        "--images-start-row",
        type=int,
        default=0,
        help="Row number to start downloading images from (0-based).",
    )
    parser.add_argument(
        "--images-max-rows",
        type=int,
        default=None,
        help="Maximum rows for the image download step (default: all).",
    )
    parser.add_argument(
        "--images-sleep",
        type=float,
        default=0.25,
        help="Delay between rows while downloading images.",
    )

    return parser.parse_args(argv or [])


def ensure_index_exists() -> None:
    if not INDEX_FILE.exists():
        raise FileNotFoundError(
            "Lost Art index CSV not found. Run without --skip-index or execute scrape-index.py first."
        )


def run_pipeline(args: argparse.Namespace) -> dict[str, int | None]:
    summary: dict[str, int | None] = {
        "index_rows": None,
        "detail_records": None,
        "image_rows": None,
    }

    if not args.skip_index:
        summary["index_rows"] = scrape_index(
            page_limit=args.index_page_limit,
            delay=args.index_delay,
        )
    else:
        _log("[skip] index refresh")

    needs_index = not (args.skip_details and args.skip_images)
    if needs_index:
        ensure_index_exists()

    if not args.skip_details:
        summary["detail_records"] = scrape_detail_pages(
            start_row=args.details_start_row,
            max_rows=args.details_max_rows,
            sleep=args.details_sleep,
            output_path=args.details_output,
        )
    else:
        _log("[skip] detail scraping")

    if not args.skip_images:
        summary["image_rows"] = scrape_images(
            start_row=args.images_start_row,
            max_rows=args.images_max_rows,
            sleep=args.images_sleep,
        )
    else:
        _log("[skip] image download")

    return summary


def format_summary(summary: dict[str, int | None]) -> str:
    parts: list[str] = []
    if summary["index_rows"] is not None:
        parts.append(f"Index rows: {summary['index_rows']}")
    if summary["detail_records"] is not None:
        parts.append(f"Detail rows: {summary['detail_records']}")
    if summary["image_rows"] is not None:
        parts.append(f"Image rows with assets: {summary['image_rows']}")
    return ", ".join(parts) if parts else "No steps executed."


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        summary = run_pipeline(args)
    except KeyboardInterrupt:
        sys.exit("Interrupted by user.")
    except FileNotFoundError as exc:
        sys.exit(str(exc))

    _log(f"[done] pipeline complete: {format_summary(summary)}")


if __name__ == "__main__":
    main(sys.argv[1:])
