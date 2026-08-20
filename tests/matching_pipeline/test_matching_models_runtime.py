"""Offline unit coverage for matching model adapters."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from matching_pipeline.image_matching import models


class _ArrayValue:
    def __init__(self, value):
        self.value = np.asarray(value)

    def cpu(self):
        return self

    def numpy(self):
        return self.value


def _cached_features(marker: float = 1.0) -> dict:
    return {
        "keypoints": models.torch.tensor([[[marker, marker + 1.0]]]),
        "keypoint_scores": models.torch.tensor([[0.9]]),
        "descriptors": models.torch.tensor([[[0.1, 0.2]]]),
        "image_size": models.torch.tensor([[640.0, 480.0]]),
    }


def _cache_test_extractor(
    max_num_keypoints: int = models.DEFAULT_MAX_NUM_KEYPOINTS,
):
    extractor = object.__new__(models.FeatureExtractor)
    extractor.device = "cpu"
    extractor.max_num_keypoints = max_num_keypoints
    extractor.model_configuration = {"max_num_keypoints": max_num_keypoints}
    extractor.model_fingerprint = "a" * 64
    extractor._source_fingerprints = {}
    return extractor


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

    def test_cuda_model_transfer_runs_memory_preflight(self) -> None:
        model = mock.Mock()
        moved = object()
        model.to.return_value = moved
        snapshot = mock.Mock()
        with (
            mock.patch.object(
                models,
                "log_cuda_memory",
                return_value=snapshot,
            ) as memory_log,
            mock.patch.object(
                models,
                "module_parameter_buffer_bytes",
                return_value=123,
            ),
            mock.patch.object(models, "require_cuda_memory") as require,
        ):
            result = models._move_model_to_device_with_preflight(
                model,
                "cuda:0",
                component="SuperPoint",
            )

        self.assertIs(result, moved)
        memory_log.assert_called_once_with(
            models.logger,
            context="before SuperPoint model load",
            device=models.torch.device("cuda:0"),
        )
        require.assert_called_once_with(
            snapshot,
            123,
            component="SuperPoint",
            basis="allocator_available",
        )
        model.to.assert_called_once_with("cuda:0")

    def test_failed_model_preflight_relogs_snapshot_at_error(self) -> None:
        model = mock.Mock()
        snapshot = mock.Mock()
        with (
            mock.patch.object(
                models,
                "log_cuda_memory",
                return_value=snapshot,
            ) as memory_log,
            mock.patch.object(
                models,
                "module_parameter_buffer_bytes",
                return_value=123,
            ),
            mock.patch.object(
                models,
                "require_cuda_memory",
                side_effect=models.InsufficientGpuMemoryError("low memory"),
            ),
            self.assertRaises(models.InsufficientGpuMemoryError),
        ):
            models._move_model_to_device_with_preflight(
                model,
                "cuda:0",
                component="LightGlue",
            )

        self.assertEqual(memory_log.call_count, 2)
        self.assertEqual(memory_log.call_args.kwargs["level"], models.logging.ERROR)
        self.assertIs(memory_log.call_args.kwargs["snapshot"], snapshot)
        model.to.assert_not_called()

    def test_feature_extractor_defaults_to_1024_keypoints(self) -> None:
        self.assertEqual(models.DEFAULT_MAX_NUM_KEYPOINTS, 1024)

    def test_feature_extractor_loads_model_and_extracts_prepared_image(self) -> None:
        configured = mock.Mock()
        configured.conf = {"max_num_keypoints": 17, "descriptor_dim": 256}
        configured.state_dict.return_value = {
            "weight": models.torch.tensor([1.0, 2.0])
        }
        configured.to.return_value = configured
        configured.extract.return_value = {
            "keypoints": models.torch.tensor([[[1.5, 2.5]]]),
            "image_size": models.torch.tensor([[4.0, 3.0]]),
        }
        superpoint = mock.Mock()
        superpoint.eval.return_value = configured
        image = mock.Mock()
        moved_image = object()
        image.to.return_value = moved_image
        prepared_path = Path(__file__)
        prepared = models.PreparedImage(
            path=prepared_path,
            pixels=np.zeros((3, 4, 3), dtype=np.uint8),
            original_size=(8, 6),
            resized_size=(4, 3),
            source_signature=models._stat_signature(prepared_path.stat()),
        )

        with (
            mock.patch.object(models, "_inference_device", return_value="cpu"),
            mock.patch.object(
                models, "SuperPoint", return_value=superpoint
            ) as factory,
            mock.patch.object(
                models,
                "numpy_image_to_torch",
                return_value=image,
            ),
            mock.patch.object(models, "perf_counter", side_effect=[2.0, 3.5]),
        ):
            extractor = models.FeatureExtractor(max_num_keypoints=17)
            result = extractor.extract_prepared(prepared)

        factory.assert_called_once_with(max_num_keypoints=17)
        configured.to.assert_called_once_with("cpu")
        image.to.assert_called_once_with("cpu")
        configured.extract.assert_called_once_with(
            moved_image, resize=None, side="long"
        )
        models.torch.testing.assert_close(
            result["keypoints"],
            models.torch.tensor([[[3.5, 5.5]]]),
        )
        models.torch.testing.assert_close(
            result["image_size"],
            models.torch.tensor([[8.0, 6.0]]),
        )
        self.assertEqual(extractor.max_num_keypoints, 17)
        self.assertEqual(extractor.model_configuration["descriptor_dim"], 256)
        self.assertEqual(len(extractor.model_fingerprint), 64)

    def test_prepare_image_resizes_on_cpu_before_cuda_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "large.jpg"
            Image.new("RGB", (16, 8), "red").save(image_path)

            prepared = models.prepare_image(image_path, resize=8)

        self.assertEqual(prepared.original_size, (16, 8))
        self.assertEqual(prepared.resized_size, (8, 4))
        self.assertEqual(prepared.pixels.shape, (4, 8, 3))
        self.assertEqual(prepared.pixels.dtype, np.uint8)

    def test_lightglue_loader_accepts_extensionless_jpeg_by_signature(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "8262f768ca77afc3990dac4aa5d9dc1d6ae8d916"
            Image.new("RGB", (8, 6), "red").save(image_path, format="JPEG")

            image = models.load_image(image_path)

        self.assertEqual(image_path.suffix, "")
        self.assertEqual(tuple(image.shape), (3, 6, 8))

    def test_feature_extractor_propagates_image_load_error(self) -> None:
        extractor = object.__new__(models.FeatureExtractor)
        extractor.device = "cpu"
        extractor.model = mock.Mock()
        load_error = OSError("unreadable image")

        with (
            mock.patch.object(models, "prepare_image", side_effect=load_error),
            self.assertRaises(OSError) as raised,
        ):
            extractor.extract(Path("broken.jpg"))

        self.assertIs(raised.exception, load_error)
        extractor.model.extract.assert_not_called()

    def test_feature_cache_is_content_addressed_versioned_and_reused(self) -> None:
        extractor = _cache_test_extractor(max_num_keypoints=123)
        features = _cached_features()
        extractor.extract = mock.Mock(return_value=features)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            source.write_bytes(b"source image bytes")
            base_cache = root / "cache" / "lost-file-id.pt"
            base_cache.parent.mkdir()
            models.torch.save(_cached_features(99.0), base_cache)

            first = extractor.load_or_extract(base_cache, source, resize=456)
            cache_files = list(base_cache.parent.glob("lost-file-id.*.pt"))
            self.assertEqual(len(cache_files), 1)
            cache_path = cache_files[0]
            identity = cache_path.name.removeprefix("lost-file-id.").removesuffix(
                ".pt"
            )
            self.assertEqual(len(identity), 64)
            self.assertTrue(base_cache.exists())
            extractor.extract.assert_called_once_with(source, resize=456)

            payload = models.torch.load(
                cache_path,
                map_location="cpu",
                weights_only=True,
            )
            metadata = payload["metadata"]
            self.assertEqual(metadata["cache_schema_version"], 2)
            self.assertEqual(metadata["extractor"]["resize_backend"], "opencv")
            self.assertEqual(metadata["extractor"]["resize_interpolation"], "area")
            self.assertEqual(
                metadata["extractor"]["coordinate_transform_version"],
                "pixel-center-v1",
            )
            self.assertEqual(metadata["source"]["algorithm"], "sha256")
            self.assertEqual(len(metadata["source"]["digest"]), 64)
            self.assertEqual(metadata["source"]["size"], len(b"source image bytes"))
            self.assertEqual(
                metadata["extractor"]["name"], models.SUPERPOINT_MODEL_NAME
            )
            self.assertEqual(
                metadata["extractor"]["model_version"],
                models.SUPERPOINT_MODEL_VERSION,
            )
            self.assertEqual(metadata["extractor"]["max_num_keypoints"], 123)
            self.assertEqual(metadata["extractor"]["resize"], 456)
            self.assertEqual(metadata["extractor"]["device_type"], "cpu")

            extractor.extract.reset_mock()
            second = extractor.load_or_extract(base_cache, source, resize=456)

        self.assertIs(first, features)
        self.assertEqual(
            models._tensor_mapping_digest(second),
            models._tensor_mapping_digest(features),
        )
        extractor.extract.assert_not_called()

    def test_feature_cache_identity_changes_with_source_model_and_resize(self) -> None:
        extractor = _cache_test_extractor()
        extractor.extract = mock.Mock(
            side_effect=lambda *_args, **_kwargs: _cached_features()
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            source.write_bytes(b"first")
            base_cache = root / "cache" / "lost.pt"

            extractor.load_or_extract(base_cache, source, resize=720)
            source.write_bytes(b"second")
            extractor.load_or_extract(base_cache, source, resize=720)
            extractor.max_num_keypoints = 4096
            extractor.model_configuration = {"max_num_keypoints": 4096}
            extractor.model_fingerprint = "b" * 64
            extractor.load_or_extract(base_cache, source, resize=None)

            cache_files = list(base_cache.parent.glob("lost.*.pt"))

        self.assertEqual(len(cache_files), 3)
        self.assertEqual(extractor.extract.call_count, 3)

    def test_corrupt_feature_cache_is_regenerated_at_same_identity(self) -> None:
        extractor = _cache_test_extractor()
        extractor.extract = mock.Mock(return_value=_cached_features(1.0))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            source.write_bytes(b"unchanged")
            base_cache = root / "cache" / "lost.pt"
            extractor.load_or_extract(base_cache, source)
            cache_path = next(base_cache.parent.glob("lost.*.pt"))
            cache_path.write_bytes(b"interrupted torch archive")

            replacement = _cached_features(2.0)
            extractor.extract.reset_mock()
            extractor.extract.return_value = replacement
            with self.assertLogs(models.logger, level="WARNING"):
                regenerated = extractor.load_or_extract(base_cache, source)
            extractor.extract.assert_called_once_with(source, resize=720)

            extractor.extract.reset_mock()
            cached = extractor.load_or_extract(base_cache, source)
            cache_files = list(base_cache.parent.glob("lost.*.pt"))

        self.assertIs(regenerated, replacement)
        self.assertEqual(
            models._tensor_mapping_digest(cached),
            models._tensor_mapping_digest(replacement),
        )
        self.assertEqual(len(cache_files), 1)
        extractor.extract.assert_not_called()

    def test_cache_preflight_file_error_is_treated_as_cache_miss(self) -> None:
        extractor = _cache_test_extractor()
        with mock.patch.object(
            extractor,
            "_fingerprint",
            side_effect=OSError("source temporarily unavailable"),
        ), self.assertLogs(models.logger, level="WARNING"):
            available = extractor.has_compatible_feature_cache(
                Path("cache.pt"),
                Path("source.jpg"),
            )

        self.assertFalse(available)

    def test_feature_cache_cuda_oom_is_fatal_not_treated_as_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "features.pt"
            cache_path.write_bytes(b"present")
            with mock.patch.object(
                models.torch,
                "load",
                side_effect=models.torch.cuda.OutOfMemoryError("cache oom"),
            ), self.assertRaises(models.torch.cuda.OutOfMemoryError):
                models._load_feature_cache(
                    cache_path,
                    expected_metadata={},
                    device=models.torch.device("cuda:0"),
                )

    def test_feature_cache_validation_cuda_oom_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "features.pt"
            cache_path.write_bytes(b"present")
            payload = {"metadata": {}, "features": {}}
            with (
                mock.patch.object(models.torch, "load", return_value=payload),
                mock.patch.object(
                    models,
                    "_validate_features",
                    side_effect=models.torch.cuda.OutOfMemoryError("validate oom"),
                ),
                self.assertRaises(models.torch.cuda.OutOfMemoryError),
            ):
                models._load_feature_cache(
                    cache_path,
                    expected_metadata={},
                    device=models.torch.device("cuda:0"),
                )

    def test_feature_cache_rejects_loadable_invalid_features(self) -> None:
        extractor = _cache_test_extractor()
        metadata = extractor._cache_metadata(
            source_fingerprint={
                "algorithm": "sha256",
                "digest": "f" * 64,
                "size": 10,
            },
            resize=720,
        )
        valid_features = _cached_features()
        wrong_shape = _cached_features()
        wrong_shape["keypoints"] = models.torch.tensor([[1.0, 2.0]])
        payloads = (
            {
                "metadata": metadata,
                "features_sha256": "0" * 64,
                "features": {},
            },
            {
                "metadata": metadata,
                "features_sha256": "0" * 64,
                "features": valid_features,
            },
            {
                "metadata": metadata,
                "features_sha256": models._tensor_mapping_digest(wrong_shape),
                "features": wrong_shape,
            },
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, payload in enumerate(payloads):
                with self.subTest(index=index):
                    cache_path = root / f"invalid-{index}.pt"
                    models.torch.save(payload, cache_path)
                    with self.assertLogs(models.logger, level="WARNING"):
                        loaded = models._load_feature_cache(
                            cache_path,
                            expected_metadata=metadata,
                            device="cpu",
                        )
                    self.assertIsNone(loaded)

    def test_feature_cache_atomic_write_preserves_target_on_failure(self) -> None:
        metadata = {"cache_schema_version": 1}
        features = _cached_features()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "lost.identity.pt"
            target.write_bytes(b"existing complete cache")

            with mock.patch.object(
                models.torch, "save", side_effect=OSError("save failed")
            ):
                with self.assertRaisesRegex(OSError, "save failed"):
                    models._write_feature_cache_atomic(
                        target,
                        metadata=metadata,
                        features=features,
                    )
            self.assertEqual(target.read_bytes(), b"existing complete cache")
            self.assertEqual(list(root.glob(f".{target.name}.tmp.*")), [])

            with mock.patch.object(
                models.os, "replace", side_effect=OSError("replace failed")
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    models._write_feature_cache_atomic(
                        target,
                        metadata=metadata,
                        features=features,
                    )
            self.assertEqual(target.read_bytes(), b"existing complete cache")
            self.assertEqual(list(root.glob(f".{target.name}.tmp.*")), [])

    def test_feature_cache_skips_write_and_rejects_source_races(self) -> None:
        extractor = _cache_test_extractor()
        features = _cached_features()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            source.write_bytes(b"original")
            base_cache = root / "cache" / "lost.pt"
            extractor.extract = mock.Mock(return_value=features)

            result = extractor.load_or_extract(
                base_cache,
                source,
                save_missing_feats=False,
            )
            self.assertFalse(base_cache.parent.exists())

            def mutate_source(_path, *, resize):
                source.write_bytes(b"changed during extraction")
                return features

            extractor.extract = mock.Mock(side_effect=mutate_source)
            with self.assertRaisesRegex(RuntimeError, "changed during"):
                extractor.load_or_extract(base_cache, source)
            self.assertFalse(base_cache.parent.exists())

        self.assertIs(result, features)

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
