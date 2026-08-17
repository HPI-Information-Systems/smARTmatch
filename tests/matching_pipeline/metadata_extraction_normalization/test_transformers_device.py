"""Transformers metadata stages must honor the configured inference device."""

from __future__ import annotations

import sys
import types
from unittest import mock

import pytest

from matching_pipeline.metadata_extraction import qwen_extract_information
from matching_pipeline.metadata_normalization.dating_normalization import (
    qwen_classify_mentions,
)
from matching_pipeline.metadata_normalization.dimension_normalization import (
    qwen_extract_dimensions,
)


@pytest.mark.parametrize(
    ("runner", "expected_extra_kwargs"),
    [
        (qwen_extract_information._run_transformers, {"trust_remote_code": True}),
        (qwen_classify_mentions._run_transformers, {}),
        (qwen_extract_dimensions._run_transformers, {"trust_remote_code": True}),
    ],
)
def test_transformers_pipeline_receives_configured_device(
    runner, expected_extra_kwargs
):
    tokenizer = object()
    tokenizer_factory = mock.Mock()
    tokenizer_factory.from_pretrained.return_value = tokenizer
    pipeline_factory = mock.Mock(return_value=mock.Mock())
    transformers = types.ModuleType("transformers")
    transformers.AutoTokenizer = tokenizer_factory
    transformers.pipeline = pipeline_factory

    with mock.patch.dict(sys.modules, {"transformers": transformers}):
        result = runner([], "test/model", "cuda:1", 128)

    assert result == []
    tokenizer_factory.from_pretrained.assert_called_once_with(
        "test/model", trust_remote_code=True
    )
    pipeline_factory.assert_called_once_with(
        "text-generation",
        model="test/model",
        tokenizer=tokenizer,
        device="cuda:1",
        **expected_extra_kwargs,
    )
