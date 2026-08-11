"""Unit tests for DINO adapter preprocessing and inference behavior."""

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

import numpy as np
import torch

from matching_pipeline.image_blocking import dino_adapter
from tests.matching_pipeline._blocking_dino_db_test_support import bare_adapter


class DinoAdapterBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = bare_adapter()

    def test_image_size_variants(self) -> None:
        for configured, expected in (
            ((224, 320), 224),
            ([128], 128),
            (64, 64),
            ("bad", 518),
            ([], 518),
        ):
            with self.subTest(configured=configured):
                self.adapter.model.config.image_size = configured
                self.assertEqual(self.adapter._image_size(), expected)
        del self.adapter.model.config.image_size
        self.assertEqual(self.adapter._image_size(), 518)

    def test_manual_transform_build_and_missing_torchvision(self) -> None:
        calls: list[tuple] = []

        def operation(name):
            return lambda *args, **kwargs: calls.append((name, args, kwargs)) or name

        transforms = types.SimpleNamespace(
            Compose=lambda values: ("compose", values),
            Resize=operation("resize"),
            CenterCrop=operation("crop"),
            ToTensor=operation("tensor"),
            Normalize=operation("normalize"),
        )
        torchvision = types.ModuleType("torchvision")
        torchvision.transforms = transforms
        transform_module = types.ModuleType("torchvision.transforms")
        transform_module.InterpolationMode = types.SimpleNamespace(BICUBIC="bicubic")
        self.adapter.model.config.image_size = 256
        with mock.patch.dict(
            sys.modules,
            {
                "torchvision": torchvision,
                "torchvision.transforms": transform_module,
            },
        ):
            built = self.adapter._build_manual_transform()
        self.assertEqual(built[0], "compose")
        self.assertEqual(calls[0], ("resize", (256,), {"interpolation": "bicubic"}))
        self.assertEqual(calls[-1][0], "normalize")

        with mock.patch.dict(sys.modules, {"torchvision": None}):
            with self.assertRaisesRegex(ImportError, "requires torchvision"):
                self.adapter._build_manual_transform()

    def test_prepare_images_and_processor_inputs(self) -> None:
        images = [object(), object()]
        self.assertIs(self.adapter._prepare_images(images), images)

        geometry = mock.Mock()
        geometry.apply.side_effect = ["first", "second"]
        self.adapter.geometry = geometry
        self.adapter.model.config.image_size = 32
        self.assertEqual(self.adapter._prepare_images(images), ["first", "second"])
        geometry.apply.assert_has_calls(
            [
                mock.call(images[0], (32, 32), self.adapter.PAD_COLOR),
                mock.call(images[1], (32, 32), self.adapter.PAD_COLOR),
            ]
        )

        moved = mock.Mock()
        moved.to.return_value = "on-device"
        self.adapter.geometry = None
        self.adapter.processor = mock.Mock(
            return_value={"pixel_values": moved, "metadata": "unchanged"}
        )
        result = self.adapter._prepare_inputs(images)
        self.assertEqual(
            result, {"pixel_values": "on-device", "metadata": "unchanged"}
        )
        moved.to.assert_called_once_with("cpu")

    def test_prepare_manual_inputs(self) -> None:
        self.adapter.processor = None
        self.adapter._manual_transform = lambda image: torch.tensor([float(image)])
        result = self.adapter._prepare_inputs([1, 2])
        torch.testing.assert_close(result["pixel_values"], torch.tensor([[1.0], [2.0]]))

    def test_pool_hidden_state_and_numpy_conversion(self) -> None:
        self.adapter.model.config.num_register_tokens = 1
        hidden = torch.tensor(
            [[[3.0, 4.0], [99.0, 99.0], [1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]]
        )
        pooled = self.adapter._pool_hidden_state(
            hidden, ["default", "cls", "avg", "median", "min", "max", "mean"]
        )
        self.assertEqual(set(pooled), {"default", "cls", "avg", "median", "min", "max"})
        for value in pooled.values():
            torch.testing.assert_close(value.norm(dim=-1), torch.ones(1))
        arrays = self.adapter._to_numpy_batch(pooled)
        self.assertTrue(all(value.dtype == np.float32 for value in arrays.values()))
        self.assertEqual(self.adapter._patch_token_start(), 2)
        self.adapter.model.config.num_register_tokens = None
        self.assertEqual(self.adapter._patch_token_start(), 1)

    def test_generate_embedding_file_and_pil_return_shapes(self) -> None:
        opened = mock.Mock()
        rgb = mock.Mock(name="rgb")
        opened.convert.return_value = rgb
        self.adapter.generate_embeddings_batch_from_pil = mock.Mock(
            side_effect=[np.array([[1.0, 2.0]]), {"avg": np.array([[3.0, 4.0]])}]
        )
        with mock.patch.object(dino_adapter.Image, "open", return_value=opened) as open_image:
            default = self.adapter.generate_embedding("image.jpg")
            pooled = self.adapter.generate_embedding_from_pil(rgb, "avg")
        open_image.assert_called_once_with("image.jpg")
        opened.convert.assert_called_once_with("RGB")
        np.testing.assert_array_equal(default, [1.0, 2.0])
        np.testing.assert_array_equal(pooled["avg"], [3.0, 4.0])

    def test_generate_batch_paths_and_model_inference(self) -> None:
        opened = [mock.Mock(), mock.Mock()]
        converted = [mock.Mock(name="rgb1"), mock.Mock(name="rgb2")]
        for source, rgb in zip(opened, converted, strict=True):
            source.convert.return_value = rgb
        self.adapter.generate_embeddings_batch_from_pil = mock.Mock(return_value="batch")
        with mock.patch.object(dino_adapter.Image, "open", side_effect=opened):
            self.assertEqual(
                self.adapter.generate_embeddings_batch(["one.jpg", "two.jpg"], "max"),
                "batch",
            )
        self.adapter.generate_embeddings_batch_from_pil.assert_called_once_with(
            converted, "max"
        )

        self.adapter.generate_embeddings_batch_from_pil = (
            dino_adapter.DinoV3Adapter.generate_embeddings_batch_from_pil.__get__(
                self.adapter
            )
        )
        self.adapter._prepare_inputs = mock.Mock(
            return_value={"pixel_values": torch.ones(2, 1)}
        )
        self.adapter.model.output = types.SimpleNamespace(
            last_hidden_state=torch.tensor(
                [
                    [[3.0, 4.0], [1.0, 0.0]],
                    [[0.0, 2.0], [0.0, 1.0]],
                ]
            )
        )
        default = self.adapter.generate_embeddings_batch_from_pil(converted)
        np.testing.assert_allclose(default, [[0.6, 0.8], [0.0, 1.0]])
        pooled = self.adapter.generate_embeddings_batch_from_pil(converted, ["max"])
        np.testing.assert_allclose(pooled["max"], [[1.0, 0.0], [0.0, 1.0]])

    def test_metadata_accessors(self) -> None:
        self.adapter.model_id = "org/model-name"
        self.adapter.model.config.hidden_size = "42"
        self.assertEqual(self.adapter.get_model_name(), "org_model_name")
        self.assertEqual(self.adapter.get_dimension(), 42)


if __name__ == "__main__":
    unittest.main()
