"""Small fakes shared by DINO adapter and blocking database tests."""

from __future__ import annotations

import types

from matching_pipeline.image_blocking import dino_adapter


class FakeModel:
    def __init__(self) -> None:
        self.config = types.SimpleNamespace(image_size=16, hidden_size=2)
        self.to_device = None
        self.eval_called = False
        self.output = None

    def to(self, device: str) -> FakeModel:
        self.to_device = device
        return self

    def eval(self) -> None:
        self.eval_called = True

    def __call__(self, **_inputs):
        return self.output


def bare_adapter() -> dino_adapter.DinoV3Adapter:
    adapter = dino_adapter.DinoV3Adapter.__new__(dino_adapter.DinoV3Adapter)
    adapter.size_key = "s"
    adapter.model_id = "fake/model"
    adapter.device = "cpu"
    adapter.hf_token = None
    adapter.geometry = None
    adapter.model = FakeModel()
    adapter.processor = None
    adapter._manual_transform = None
    return adapter


class FakeCursor:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.sql = ""
        self.params = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_exc_info) -> None:
        return None

    def execute(self, sql: str, params) -> None:
        self.sql = sql
        self.params = params


class FakeConnection:
    def __init__(self, rowcount: int) -> None:
        self.cursor_value = FakeCursor(rowcount)
        self.committed = False

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_exc_info) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_value

    def commit(self) -> None:
        self.committed = True
