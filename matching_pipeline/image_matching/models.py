"""SuperPoint, LightGlue, and classifier adapters for final image verification."""

import logging
import os
from pathlib import Path
from time import perf_counter

import joblib
import numpy as np
import torch
from lightglue import LightGlue, SuperPoint
from lightglue.utils import load_image, rbd

logger = logging.getLogger(__name__)


def _inference_device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


class FeatureExtractor:
    def __init__(self, max_num_keypoints: int = 2048) -> None:
        started_at = perf_counter()
        self.device = _inference_device()
        logger.info(
            "Loading SuperPoint feature extractor (max_num_keypoints=%d, device=%s)",
            max_num_keypoints,
            self.device,
        )
        self.model = (
            SuperPoint(max_num_keypoints=max_num_keypoints).eval().to(self.device)
        )
        logger.info(
            "SuperPoint feature extractor ready in %.1fs on %s",
            perf_counter() - started_at,
            self.device,
        )

    def extract(self, path: Path, resize: int | None = 720) -> dict:
        try:
            image = load_image(path).to(self.device)
        except OSError as exc:
            logger.warning(
                "Could not load image %s; using blank fallback image: %s",
                path,
                exc,
            )
            image = torch.zeros([3, 1, 1], device=self.device)
        feats = self.model.extract(image, resize=resize, side="long")
        return feats

    def load_or_extract(
        self,
        feats_path: Path,
        image_path: Path,
        save_missing_feats: bool = True,
        resize: int | None = 720,
    ) -> dict:
        if feats_path.exists():
            feats = torch.load(feats_path)
        else:
            feats = self.extract(image_path, resize=resize)
            if save_missing_feats:
                os.makedirs(feats_path.parent, exist_ok=True)
                torch.save(feats, feats_path)
        return feats


class FeatureMatcher:
    def __init__(self, features: str = "superpoint") -> None:
        started_at = perf_counter()
        self.device = _inference_device()
        logger.info(
            "Loading LightGlue matcher (features=%s, device=%s)", features, self.device
        )
        self.model = LightGlue(features=features).eval().to(self.device)
        logger.info(
            "LightGlue matcher ready in %.1fs on %s",
            perf_counter() - started_at,
            self.device,
        )

    @torch.no_grad()
    def match(self, feats0: dict, feats1: dict) -> dict:
        matches01 = self.model({"image0": feats0, "image1": feats1})
        matches01 = rbd(matches01)
        return matches01


class MatchClassifier:
    def __init__(
        self, model_path: Path = Path(__file__).parent.absolute() / "classifier.pkl"
    ) -> None:
        started_at = perf_counter()
        logger.info("Loading match classifier: %s", model_path)
        self.model = joblib.load(model_path)
        logger.info("Match classifier ready in %.1fs", perf_counter() - started_at)

    def __extract_match_features(self, matches: dict) -> np.ndarray:
        scores = matches["scores"].cpu().numpy()
        if scores.size == 0:
            scores = np.array([0.0])
        return np.array(
            [
                len(scores),
                np.mean(scores),
                np.var(scores),
                np.std(scores),
                np.min(scores),
                np.max(scores),
                np.ptp(scores),
                np.quantile(scores, q=0.1),
                np.quantile(scores, q=0.2),
                np.quantile(scores, q=0.3),
                np.quantile(scores, q=0.4),
                np.quantile(scores, q=0.5),
                np.quantile(scores, q=0.6),
                np.quantile(scores, q=0.7),
                np.quantile(scores, q=0.8),
                np.quantile(scores, q=0.9),
                np.sum(scores),
            ]
        )

    def classify_matches(self, matches: dict) -> tuple[bool, float]:
        features = np.array([self.__extract_match_features(matches)]).reshape(1, -1)
        prediction = self.model.predict(features)[0]
        confidence = self.model.predict_proba(features)[0][1]

        return prediction, confidence
