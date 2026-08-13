"""One-shot scraper worker commands used by manual and scheduled triggers."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from shared.logging_adapter import configure_logging, get_logger
from scrapers.orchestrator import Orchestrator, SCRAPER_REGISTRY
from scrapers.scope import DASHBOARD_SCRAPER_NAMES

SOURCE_CHOICES = ("manual", "scheduled", "startup", "cli")
_REPO_ROOT = Path(__file__).resolve().parents[1]
logger = get_logger(__name__)


def _worker_orchestrator() -> Orchestrator:
    return Orchestrator(reconcile_interrupted_runs=False)


def run_one(
    scraper_name: str,
    *,
    source: str,
    orchestrator_factory: Callable[[], Any] = _worker_orchestrator,
) -> int:
    """Run one scraper synchronously and return a process exit code."""
    logger.info("source=%s scraper=%s starting", source, scraper_name)
    try:
        result = orchestrator_factory().run_scraper(scraper_name)
    except Exception:
        logger.exception("source=%s scraper=%s launch failed", source, scraper_name)
        return 1

    logger.info("result=%s", json.dumps(result, sort_keys=True, default=str))
    status = result.get("status")
    if status == "failed":
        logger.error(
            "source=%s scraper=%s failed result=%s",
            source,
            scraper_name,
            json.dumps(result, sort_keys=True, default=str),
        )
        return 1
    if status == "skipped":
        logger.info("source=%s scraper=%s already running; skipped", source, scraper_name)
    return 0


def _run_one_command(scraper_name: str, source: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "scrapers.worker",
        "run",
        scraper_name,
        "--source",
        source,
    ]


def run_batch(
    scraper_names: Sequence[str] = DASHBOARD_SCRAPER_NAMES,
    *,
    source: str,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> int:
    """Start all requested scraper processes, then wait and reap every one."""
    children: list[tuple[str, Any]] = []
    failed = False

    for scraper_name in scraper_names:
        command = _run_one_command(scraper_name, source)
        try:
            process = popen_factory(command, cwd=str(_REPO_ROOT))
        except OSError:
            failed = True
            logger.exception("scraper=%s could not start", scraper_name)
            continue
        children.append((scraper_name, process))

    with ThreadPoolExecutor(max_workers=max(1, len(children))) as executor:
        pending = {
            executor.submit(process.wait): scraper_name
            for scraper_name, process in children
        }
        for future in as_completed(pending):
            scraper_name = pending[future]
            try:
                return_code = future.result()
            except Exception:
                failed = True
                logger.exception("scraper=%s wait failed", scraper_name)
                continue
            if return_code != 0:
                failed = True
                logger.error(
                    "scraper=%s failed with exit code %s", scraper_name, return_code
                )

    log = logger.error if failed else logger.info
    log(
        "source=%s batch finished status=%s",
        source,
        "failed" if failed else "completed",
    )
    return 1 if failed else 0


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one registered scraper.")
    run_parser.add_argument("scraper", choices=tuple(SCRAPER_REGISTRY))
    run_parser.add_argument("--source", choices=SOURCE_CHOICES, default="cli")

    batch_parser = subparsers.add_parser(
        "run-all",
        help="Run the dashboard's auction scraper set concurrently.",
    )
    batch_parser.add_argument("--source", choices=SOURCE_CHOICES, default="cli")

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    args = _parse_args(argv)
    if args.command == "run":
        return run_one(args.scraper, source=args.source)
    return run_batch(source=args.source)


if __name__ == "__main__":
    raise SystemExit(main())
