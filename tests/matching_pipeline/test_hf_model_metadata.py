from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from matching_pipeline.shared import hf_model_metadata


class HfModelMetadataTests(unittest.TestCase):
    def test_successful_response_is_cached_once_per_model_revision(self) -> None:
        info = types.SimpleNamespace(
            sha="resolved-sha",
            safetensors=types.SimpleNamespace(
                parameters={"F32": 100, "F16": 20},
                total=120,
            ),
        )
        api = mock.Mock()
        api.model_info.return_value = info
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            hf_model_metadata,
            "HfApi",
            return_value=api,
        ):
            cache_dir = Path(directory)
            first = hf_model_metadata.get_cached_hf_safetensors_metadata(
                "org/model",
                token="secret",
                cache_dir=cache_dir,
            )
            second = hf_model_metadata.get_cached_hf_safetensors_metadata(
                "org/model",
                token="different-token",
                cache_dir=cache_dir,
            )
            cache_files = list(cache_dir.glob("*.json"))

        self.assertEqual(first, second)
        self.assertEqual(first.resolved_revision, "resolved-sha")
        self.assertEqual(first.parameter_counts_by_dtype, {"F32": 100, "F16": 20})
        api.model_info.assert_called_once_with(
            "org/model",
            revision="main",
            token="secret",
            timeout=hf_model_metadata._MODEL_INFO_TIMEOUT_SECONDS,
            expand=["safetensors", "sha"],
        )
        self.assertEqual(len(cache_files), 1)
        self.assertNotIn("org", cache_files[0].name)

    def test_requested_revisions_use_distinct_cache_entries(self) -> None:
        api = mock.Mock()
        api.model_info.side_effect = [
            types.SimpleNamespace(
                sha="sha-one",
                safetensors=types.SimpleNamespace(
                    parameters={"F32": 10},
                    total=10,
                ),
            ),
            types.SimpleNamespace(
                sha="sha-two",
                safetensors=types.SimpleNamespace(
                    parameters={"F32": 10},
                    total=10,
                ),
            ),
        ]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            hf_model_metadata,
            "HfApi",
            return_value=api,
        ):
            cache_dir = Path(directory)
            first = hf_model_metadata.get_cached_hf_safetensors_metadata(
                "org/model",
                revision="release-one",
                token=None,
                cache_dir=cache_dir,
            )
            second = hf_model_metadata.get_cached_hf_safetensors_metadata(
                "org/model",
                revision="release-two",
                token=None,
                cache_dir=cache_dir,
            )
            cache_files = list(cache_dir.glob("*.json"))

        self.assertEqual(first.resolved_revision, "sha-one")
        self.assertEqual(second.resolved_revision, "sha-two")
        self.assertEqual(api.model_info.call_count, 2)
        self.assertEqual(len(cache_files), 2)

    def test_corrupt_cache_is_replaced_atomically(self) -> None:
        info = types.SimpleNamespace(
            sha="resolved-sha",
            safetensors=types.SimpleNamespace(
                parameters={"F32": 50},
                total=50,
            ),
        )
        api = mock.Mock()
        api.model_info.return_value = info
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            hf_model_metadata,
            "HfApi",
            return_value=api,
        ):
            cache_dir = Path(directory)
            identity = hf_model_metadata.hashlib.sha256(
                b"org/model\0main"
            ).hexdigest()
            cache_path = cache_dir / f"{identity}.json"
            cache_path.write_text("not json", encoding="utf-8")

            metadata = hf_model_metadata.get_cached_hf_safetensors_metadata(
                "org/model",
                token=None,
                cache_dir=cache_dir,
            )
            payload = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(metadata.total_parameters, 50)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["resolved_revision"], "resolved-sha")
        api.model_info.assert_called_once()

    def test_inconsistent_hub_metadata_is_rejected_and_not_cached(self) -> None:
        info = types.SimpleNamespace(
            sha="resolved-sha",
            safetensors=types.SimpleNamespace(
                parameters={"F32": 50},
                total=51,
            ),
        )
        api = mock.Mock()
        api.model_info.return_value = info
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            hf_model_metadata,
            "HfApi",
            return_value=api,
        ), self.assertRaisesRegex(ValueError, "Inconsistent"):
            hf_model_metadata.get_cached_hf_safetensors_metadata(
                "org/model",
                token=None,
                cache_dir=Path(directory),
            )

        self.assertEqual(list(Path(directory).glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
