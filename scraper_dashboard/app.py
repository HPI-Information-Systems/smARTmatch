"""anna.austen Scraper Dashboard.

A Flask web application that provides a clean, academic-style UI for
monitoring and controlling the anna.austen scrapers via the orchestrator.

Usage:
    python -m scraper_dashboard.app
    # or
    cd scraper_dashboard && python app.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

# Ensure the repo root is importable so ``from scrapers.orchestrator import …`` works
# regardless of how the dashboard is launched.
_REPO_ROOT_PATH = Path(__file__).resolve().parents[1]
_REPO_ROOT = str(_REPO_ROOT_PATH)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from flask import Flask, jsonify, render_template, request  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from werkzeug.exceptions import BadRequest  # noqa: E402

from scraper_dashboard.storage_stats import build_storage_stats  # noqa: E402
from scrapers.db_interface import Database  # noqa: E402
from scrapers.orchestrator import Orchestrator, SCRAPER_REGISTRY  # noqa: E402
from scrapers.process_launcher import WorkerProcessLauncher  # noqa: E402
from scrapers.runtime_config import save_request_cooldown_override  # noqa: E402
from scrapers.scope import DASHBOARD_SCRAPER_NAMES  # noqa: E402


app = Flask(__name__)
orch = Orchestrator()
storage_stats_db = Database()
worker_launcher = WorkerProcessLauncher()
_STORAGE_STATS_CACHE_TTL_SECONDS = 60
_storage_stats_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_last_good_statuses: list[dict] | None = None
_CONFIG_UPDATE_LOCK = threading.Lock()


def _is_ui_scraper(scraper_name: str) -> bool:
    return scraper_name in DASHBOARD_SCRAPER_NAMES


def _ui_scraper_names() -> list[str]:
    return [name for name in DASHBOARD_SCRAPER_NAMES if name in SCRAPER_REGISTRY]


def _get_storage_stats(scraper_name: str) -> dict[str, Any]:
    now = time.monotonic()
    cached = _storage_stats_cache.get(scraper_name)
    if cached and (now - cached[0]) < _STORAGE_STATS_CACHE_TTL_SECONDS:
        return cached[1]

    stats = build_storage_stats(
        db=storage_stats_db,
        repo_root=_REPO_ROOT_PATH,
        scraper_name=scraper_name,
        scraper_info=SCRAPER_REGISTRY.get(scraper_name, {}),
    )
    _storage_stats_cache[scraper_name] = (now, stats)
    return stats


def _enrich_statuses_with_storage(statuses: list[dict]) -> list[dict]:
    enriched: list[dict] = []
    for status in statuses:
        scraper_name = status.get("name")
        if not scraper_name:
            enriched.append(status)
            continue

        enriched.append({**status, **_get_storage_stats(scraper_name)})
    return enriched


def _dashboard_statuses() -> list[dict]:
    global _last_good_statuses
    allowed = set(_ui_scraper_names())
    try:
        statuses = [status for status in orch.get_all_status() if status.get("name") in allowed]
        enriched = _enrich_statuses_with_storage(statuses)
    except OperationalError:
        # DB is unreachable; serve last known statuses (or empty) so the polling
        # frontend doesn't pin Waitress threads with 500s
        if _last_good_statuses is not None:
            return [{**status, "db_unavailable": True} for status in _last_good_statuses]
        return []
    _last_good_statuses = enriched
    return enriched


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.route("/")
def dashboard():
    return render_template("dashboard.html", scrapers=_dashboard_statuses())


# ---------------------------------------------------------------------------
# API endpoints (called from frontend JS and usable standalone)
# ---------------------------------------------------------------------------


@app.route("/api/status")
def api_status():
    """Return current status of all scrapers."""
    return jsonify(_dashboard_statuses())


@app.route("/api/history")
def api_history():
    """Return run history. Optional ?scraper=name&limit=N."""
    scraper = request.args.get("scraper")
    limit = request.args.get("limit", 50, type=int)
    return jsonify(orch.get_run_history(scraper_name=scraper, limit=limit))


@app.route("/api/run/<scraper_name>", methods=["POST"])
def api_run_scraper(scraper_name: str):
    """Submit one finite scraper worker process."""
    if scraper_name not in SCRAPER_REGISTRY or not _is_ui_scraper(scraper_name):
        return jsonify({"error": f"Unknown scraper: {scraper_name}"}), 404
    try:
        result = worker_launcher.launch_scraper(scraper_name)
    except OSError as exc:
        return jsonify({"error": f"Could not launch scraper worker: {exc}"}), 503
    return jsonify(result), 202


def run_dashboard_scrapers_background() -> dict[str, Any]:
    """Submit the same finite batch process used by the interval scheduler."""
    return worker_launcher.launch_all()


@app.route("/api/run-all", methods=["POST"])
def api_run_all():
    """Submit all dashboard scrapers as a one-shot child process."""
    try:
        result = run_dashboard_scrapers_background()
    except OSError as exc:
        return jsonify({"error": f"Could not launch scraper batch: {exc}"}), 503
    return jsonify(result), 202


@app.route("/api/scrapers")
def api_scrapers():
    """List registered scrapers."""
    return jsonify([
        {"name": name, "display_name": info["display_name"]}
        for name, info in SCRAPER_REGISTRY.items()
        if _is_ui_scraper(name)
    ])


@app.route("/api/config", methods=["GET"])
def api_config_get():
    """Return current orchestrator configuration."""
    return jsonify({"request_cooldown_seconds": orch.get_cooldown()})


@app.route("/api/config", methods=["POST"])
def api_config_post():
    """Update orchestrator configuration. Accepts JSON body."""
    data = request.get_json(silent=True)
    if not data:
        raise BadRequest("Expected JSON body.")

    updated: dict = {}

    if "request_cooldown_seconds" in data:
        try:
            value = float(data["request_cooldown_seconds"])
        except (TypeError, ValueError):
            raise BadRequest("request_cooldown_seconds must be a number.")
        try:
            validated = Orchestrator._validate_cooldown(
                value,
                source="request_cooldown_seconds",
            )
            with _CONFIG_UPDATE_LOCK:
                save_request_cooldown_override(validated)
                orch.set_cooldown(validated)
        except ValueError as exc:
            raise BadRequest(str(exc))
        updated["request_cooldown_seconds"] = orch.get_cooldown()

    if not updated:
        raise BadRequest("No recognised configuration keys in request body.")

    return jsonify(updated)


if __name__ == "__main__":
    from waitress import serve

    from shared.logging_adapter import configure_logging

    configure_logging()
    serve(app, host="0.0.0.0", port=5555)
