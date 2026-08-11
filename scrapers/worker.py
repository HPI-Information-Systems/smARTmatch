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

from scrapers.orchestrator import Orchestrator, SCRAPER_REGISTRY
from scrapers.scope import DASHBOARD_SCRAPER_NAMES

SOURCE_CHOICES = ("manual", "scheduled", "startup", "cli")
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _worker_orchestrator() -> Orchestrator:
    return Orchestrator(reconcile_interrupted_runs=False)


def run_one(
    scraper_name: str,
    *,
    source: str,
    orchestrator_factory: Callable[[], Any] = _worker_orchestrator,
) -> int:
    """Run one scraper synchronously and return a process exit code."""
    print(f"[worker] source={source} scraper={scraper_name} starting", flush=True)
    try:
        result = orchestrator_factory().run_scraper(scraper_name)
    except Exception as exc:
        print(
            f"[worker] source={source} scraper={scraper_name} launch failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    print(json.dumps(result, sort_keys=True, default=str), flush=True)
    status = result.get("status")
    if status == "failed":
        return 1
    if status == "skipped":
        print(
            f"[worker] source={source} scraper={scraper_name} already running; skipped",
            flush=True,
        )
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
        except OSError as exc:
            failed = True
            print(
                f"[worker] scraper={scraper_name} could not start: {exc}",
                file=sys.stderr,
                flush=True,
            )
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
            except Exception as exc:
                failed = True
                print(
                    f"[worker] scraper={scraper_name} wait failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if return_code != 0:
                failed = True
                print(
                    f"[worker] scraper={scraper_name} failed with exit code {return_code}",
                    file=sys.stderr,
                    flush=True,
                )

    print(
        f"[worker] source={source} batch finished status={'failed' if failed else 'completed'}",
        flush=True,
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
    args = _parse_args(argv)
    if args.command == "run":
        return run_one(args.scraper, source=args.source)
    return run_batch(source=args.source)


if __name__ == "__main__":
    raise SystemExit(main())
