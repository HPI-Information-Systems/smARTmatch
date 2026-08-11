""""
This module implements a confidence function for dimensions attributes,
based on the IDF-like scoring of dimensions in the lost_artwork table.
"""

import pickle
from pathlib import Path

import numpy as np
from scipy.stats import gaussian_kde

from matching_pipeline.shared.db import connect as db_connect

_CACHE_DIR = Path(__file__).parent / "__pycache__"
_CACHE_DIR.mkdir(exist_ok=True)
_DIM_CACHE = _CACHE_DIR / "dim_state.pkl"

_dim_state = None


def _load_dimensions():
    global _dim_state
    if _DIM_CACHE.exists():
        with open(_DIM_CACHE, "rb") as f:
            _dim_state = pickle.load(f)
        return

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT width, height
                FROM lost_artwork
                WHERE width IS NOT NULL AND height IS NOT NULL
                  AND width > 0 AND height > 0
                  AND width  <= (SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY width)  FROM lost_artwork WHERE width  > 0)
                  AND height <= (SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY height) FROM lost_artwork WHERE height > 0);
                """
            )
            rows = cur.fetchall()

    # 2D density estimate over (width, height).
    pts = np.array(rows, dtype=float).T
    kde = gaussian_kde(pts)

    # log(1/density): high for rare sizes, low for common ones.
    densities = kde(pts)
    log_inv = np.log(1.0 / np.maximum(densities, 1e-12))
    _dim_state = {
        "kde": kde,
        "log_inv_max": float(log_inv.max()),
    }
    with open(_DIM_CACHE, "wb") as f:
        pickle.dump(_dim_state, f)


def _score_dimensions(width: float, height: float) -> float:
    if _dim_state is None:
        _load_dimensions()

    s = _dim_state
    density = float(s["kde"](np.array([[width], [height]]))[0])
    log_inv = np.log(1.0 / max(density, 1e-12))
    return float(np.clip((log_inv / s["log_inv_max"]), 0.0, 1.0))


def confidence_function(lost_dim_conf: float, auc_dim_conf: float) -> float:
    return (lost_dim_conf + auc_dim_conf) / 2.0
