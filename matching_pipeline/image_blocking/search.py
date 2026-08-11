"""Vector search helpers for file-backed image blocking."""

from __future__ import annotations

import numpy as np

from .embedding_cache import normalize_embeddings


def topk_cosine_similarity(
    query_embeddings: np.ndarray,
    candidate_embeddings: np.ndarray,
    *,
    top_k: int,
) -> tuple[np.ndarray, np.ndarray]:
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    queries = normalize_embeddings(query_embeddings)
    candidates = normalize_embeddings(candidate_embeddings)
    if candidates.shape[0] == 0:
        raise ValueError("candidate_embeddings must not be empty")
    if queries.shape[1] != candidates.shape[1]:
        raise ValueError(
            f"Query dimension {queries.shape[1]} does not match candidate dimension {candidates.shape[1]}"
        )

    k = min(top_k, candidates.shape[0])
    scores = queries @ candidates.T
    if k == candidates.shape[0]:
        indices = np.argsort(-scores, axis=1)
    else:
        partial = np.argpartition(-scores, kth=k - 1, axis=1)[:, :k]
        partial_scores = np.take_along_axis(scores, partial, axis=1)
        order = np.argsort(-partial_scores, axis=1)
        indices = np.take_along_axis(partial, order, axis=1)
    values = np.take_along_axis(scores, indices[:, :k], axis=1)
    return indices[:, :k].astype(np.int64), values.astype(np.float32)
