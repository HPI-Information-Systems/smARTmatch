"""Shared model fake for blocking cache and candidate tests."""

from __future__ import annotations

import numpy as np


class EmbeddingModel:
    def __init__(self, vectors: dict[str, list[float]], dimension: int = 2) -> None:
        self.vectors = vectors
        self.dimension = dimension
        self.calls: list[list[str]] = []

    def get_dimension(self) -> int:
        return self.dimension

    def generate_embeddings_batch(self, paths: list[str]) -> np.ndarray:
        self.calls.append(paths)
        return np.asarray([self.vectors[path] for path in paths], dtype=np.float32)
