"""Focused selective synchronization tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telemetry import sync_workspace as sync


class _Settings:
    endpoint = "https://receiver.example/api/telemetry/sync/v3/pages"
    timeout_seconds = 10.0
    auth_token = "unit-test-static-bearer-token"


class SyncWorkspaceTests(unittest.TestCase):
    def test_scavenger_preserves_a_locked_active_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            sync.tempfile, "gettempdir", return_value=temp_dir
        ):
            with sync.sync_workspace() as active:
                sync.cleanup_stale_sync_spools()
                self.assertTrue(active.exists())

    def test_stale_spool_scavenging_removes_only_known_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            sync.tempfile, "gettempdir", return_value=temp_dir
        ):
            temp_root = Path(temp_dir)
            current = temp_root / sync._SYNC_SPOOL_DIRECTORY / "sync-stale"
            legacy = temp_root / "smartmatch-sync-stale"
            unrelated = temp_root / "keep-me"
            current.mkdir(parents=True)
            legacy.mkdir()
            os.utime(legacy, (0, 0))
            unrelated.mkdir()

            sync.cleanup_stale_sync_spools()

            self.assertFalse(current.exists())
            self.assertFalse(legacy.exists())
            self.assertTrue(unrelated.exists())
