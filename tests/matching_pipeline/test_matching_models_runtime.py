"""Offline unit coverage for matching model adapters."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from matching_pipeline.image_matching import models


class _ArrayValue:
    def __init__(self, value):
        self.value = np.asarray(value)

    def cpu(self):
        return self

    def numpy(self):
        return self.value


class ModelAdapterTests(unittest.TestCase):
    def test_inference_device_selects_cuda_and_cpu(self) -> None:
        for available, expected in ((True, "cuda"), (False, "cpu")):
            with (
                self.subTest(available=available),
                mock.patch.object(
                    models.torch.cuda, "is_available", return_value=available
                ),
                mock.patch.object(
                    models.torch, "device", side_effect=lambda value: value
                ),
            ):
                self.assertEqual(models._inference_device(), expected)

    def test_feature_extractor_loads_model_and_extracts_image(self) -> None:
        configured = mock.Mock()
        superpoint = mock.Mock()
        superpoint.eval.return_value.to.return_value = configured
        image = mock.Mock()
        moved_image = object()
        image.to.return_value = moved_image
        configured.extract.return_value = {"keypoints": "features"}

        with (
            mock.patch.object(models, "_inference_device", return_value="cpu"),
            mock.patch.object(
                models, "SuperPoint", return_value=superpoint
            ) as factory,
            mock.patch.object(models, "load_image", return_value=image),
            mock.patch.object(models, "perf_counter", side_effect=[2.0, 3.5]),
        ):
            extractor = models.FeatureExtractor(max_num_keypoints=17)
            result = extractor.extract(Path("image.jpg"), resize=None)

        factory.assert_called_once_with(max_num_keypoints=17)
        superpoint.eval.return_value.to.assert_called_once_with("cpu")
        image.to.assert_called_once_with("cpu")
        configured.extract.assert_called_once_with(
            moved_image, resize=None, side="long"
        )
        self.assertEqual(result, {"keypoints": "features"})

    def test_feature_extractor_uses_blank_image_after_load_error(self) -> None:
        extractor = object.__new__(models.FeatureExtractor)
        extractor.device = "cpu"
        extractor.model = mock.Mock()
        extractor.model.extract.return_value = {"blank": True}
        blank = object()

        with (
            mock.patch.object(models, "load_image", side_effect=OSError("bad")),
            mock.patch.object(models.torch, "zeros", return_value=blank) as zeros,
        ):
            result = extractor.extract(Path("broken.jpg"))

        zeros.assert_called_once_with([3, 1, 1], device="cpu")
        extractor.model.extract.assert_called_once_with(
            blank, resize=720, side="long"
        )
        self.assertEqual(result, {"blank": True})

    def test_load_or_extract_reads_existing_cache(self) -> None:
        extractor = object.__new__(models.FeatureExtractor)
        cached = {"cached": True}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "features.pt"
            path.touch()
            with (
                mock.patch.object(
                    models.torch, "load", return_value=cached
                ) as load,
                mock.patch.object(extractor, "extract") as extract,
            ):
                result = extractor.load_or_extract(path, Path("unused.jpg"))
        load.assert_called_once_with(path)
        extract.assert_not_called()
        self.assertIs(result, cached)

    def test_load_or_extract_can_save_or_skip_missing_cache(self) -> None:
        extractor = object.__new__(models.FeatureExtractor)
        features = {"new": True}
        extractor.extract = mock.Mock(return_value=features)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "nested" / "features.pt"
            with mock.patch.object(models.torch, "save") as save:
                result = extractor.load_or_extract(
                    cache, Path("source.jpg"), resize=123
                )
            self.assertTrue(cache.parent.is_dir())
            save.assert_called_once_with(features, cache)

            with mock.patch.object(models.torch, "save") as save:
                result_without_save = extractor.load_or_extract(
                    root / "other.pt",
                    Path("other.jpg"),
                    save_missing_feats=False,
                    resize=None,
                )
            save.assert_not_called()

        self.assertIs(result, features)
        self.assertIs(result_without_save, features)
        extractor.extract.assert_has_calls(
            [
                mock.call(Path("source.jpg"), resize=123),
                mock.call(Path("other.jpg"), resize=None),
            ]
        )

    def test_feature_matcher_loads_and_runs_lightglue(self) -> None:
        configured = mock.Mock(return_value={"raw": True})
        lightglue = mock.Mock()
        lightglue.eval.return_value.to.return_value = configured
        reduced = {"scores": [0.5]}

        with (
            mock.patch.object(models, "_inference_device", return_value="cpu"),
            mock.patch.object(
                models, "LightGlue", return_value=lightglue
            ) as factory,
            mock.patch.object(models, "rbd", return_value=reduced) as rbd,
            mock.patch.object(models, "perf_counter", side_effect=[4.0, 5.0]),
        ):
            matcher = models.FeatureMatcher(features="custom")
            result = matcher.match({"a": 1}, {"b": 2})

        factory.assert_called_once_with(features="custom")
        lightglue.eval.return_value.to.assert_called_once_with("cpu")
        configured.assert_called_once_with(
            {"image0": {"a": 1}, "image1": {"b": 2}}
        )
        rbd.assert_called_once_with({"raw": True})
        self.assertIs(result, reduced)

    def test_classifier_loads_and_classifies_regular_scores(self) -> None:
        classifier_model = mock.Mock()
        classifier_model.predict.return_value = np.array([True])
        classifier_model.predict_proba.return_value = np.array([[0.1, 0.9]])
        model_path = Path("classifier.pkl")
        with (
            mock.patch.object(
                models.joblib, "load", return_value=classifier_model
            ) as load,
            mock.patch.object(models, "perf_counter", side_effect=[7.0, 8.0]),
        ):
            classifier = models.MatchClassifier(model_path)

        prediction, confidence = classifier.classify_matches(
            {"scores": _ArrayValue([0.2, 0.6, 1.0])}
        )
        load.assert_called_once_with(model_path)
        self.assertTrue(prediction)
        self.assertEqual(confidence, 0.9)
        features = classifier_model.predict.call_args.args[0]
        self.assertEqual(features.shape, (1, 17))
        self.assertEqual(features[0, 0], 3)
        self.assertAlmostEqual(features[0, 1], 0.6)
        np.testing.assert_array_equal(
            features, classifier_model.predict_proba.call_args.args[0]
        )

    def test_classifier_replaces_empty_scores_with_zero(self) -> None:
        classifier = object.__new__(models.MatchClassifier)
        classifier.model = mock.Mock()
        classifier.model.predict.return_value = np.array([False])
        classifier.model.predict_proba.return_value = np.array([[1.0, 0.0]])
        prediction, confidence = classifier.classify_matches(
            {"scores": _ArrayValue([])}
        )
        self.assertFalse(prediction)
        self.assertEqual(confidence, 0.0)
        expected = np.zeros((1, 17))
        expected[0, 0] = 1
        np.testing.assert_array_equal(
            classifier.model.predict.call_args.args[0], expected
        )


if __name__ == "__main__":
    unittest.main()
