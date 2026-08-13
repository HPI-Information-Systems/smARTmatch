"""Frontend filesystem-status API."""

import time
from threading import Lock

from flask import jsonify
from sqlalchemy import text

from .. import app as app_module
from ..stats_storage import image_file_metrics, project_directory_metrics
from ..sql import stats as stats_sql


_PATH_CACHE_LOCK = Lock()
_PATH_CACHE = {"paths": None, "expires_at": 0.0}


def _image_paths():
    now = time.monotonic()
    with _PATH_CACHE_LOCK:
        if _PATH_CACHE["paths"] is not None and now < _PATH_CACHE["expires_at"]:
            return _PATH_CACHE["paths"]

    with app_module.engine.connect() as connection:
        rows = connection.execute(text(stats_sql.IMAGE_FILE_PATHS_SQL)).mappings()
        paths = tuple(row["file_path"] for row in rows)
    with _PATH_CACHE_LOCK:
        _PATH_CACHE.update({"paths": paths, "expires_at": now + 60})
    return paths


@app_module.app.route("/api/system-status")
def api_system_status():
    image_files = image_file_metrics(_image_paths(), refresh_async=True)
    project_size = project_directory_metrics(refresh_async=True)
    return jsonify(
        {
            "image_files": {
                "missing_count": image_files["missing_count"],
                "missing_label": image_files["missing_label"],
                "scan_ready": image_files["scan_ready"],
            },
            "project_size": project_size,
        }
    )
