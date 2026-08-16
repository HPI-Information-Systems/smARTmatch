"""Offline tests for bounded daily telemetry."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from matching_pipeline.shared import telemetry


class TelemetrySettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.images = self.root / "images"
        self.images.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {"SMARTMATCH_IMAGES_DIR": str(self.images)},
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_payload_limits_are_fixed_code_defaults(self) -> None:
        self.assertEqual(telemetry.DEFAULT_MAX_PAYLOAD_BYTES, 5_242_880)
        self.assertEqual(telemetry.DEFAULT_MAX_UNCOMPRESSED_PAYLOAD_BYTES, 20_971_520)
        self.assertEqual(telemetry.DEFAULT_MAX_MATCH_RECORDS, 5_000)

    def test_disabled_telemetry_does_not_require_an_endpoint(self) -> None:
        self.assertIsNone(telemetry.load_telemetry_settings())
        with mock.patch.object(telemetry, "connect") as connect:
            self.assertEqual(telemetry.try_send_daily_telemetry(), "disabled")
        connect.assert_not_called()

    def test_enabled_settings_require_https_and_accept_timeout(self) -> None:
        os.environ["TELEMETRY_ENABLED"] = "true"
        os.environ["TELEMETRY_ENDPOINT"] = "http://example.test/collect"
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            telemetry.load_telemetry_settings()

        os.environ["TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP"] = "true"
        with self.assertRaisesRegex(ValueError, "local hosts"):
            telemetry.load_telemetry_settings()

        os.environ["TELEMETRY_ENDPOINT"] = "http://telemetry_receiver:8080/collect"
        os.environ["TELEMETRY_AUTH_TOKEN"] = "unit-test-static-bearer-token"
        settings = telemetry.load_telemetry_settings()
        assert settings is not None
        self.assertEqual(settings.endpoint, os.environ["TELEMETRY_ENDPOINT"])

        os.environ["TELEMETRY_ENDPOINT"] = "https://example.test/collect"
        os.environ["TELEMETRY_ALLOW_INSECURE_LOCAL_HTTP"] = "false"
        os.environ["TELEMETRY_AUTH_TOKEN"] = "unit-test-static-bearer-token"
        os.environ["TELEMETRY_TIMEOUT_SECONDS"] = "2.5"
        settings = telemetry.load_telemetry_settings()
        assert settings is not None
        self.assertEqual(settings.timeout_seconds, 2.5)


class DirectoryHashTests(unittest.TestCase):
    def test_tree_and_immediate_subdirectory_hashes_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "root.jpg").write_bytes(b"root")
            child = root / "scraper-a"
            nested = child / "nested"
            nested.mkdir(parents=True)
            (child / "a.jpg").write_bytes(b"a")
            (nested / "b.jpg").write_bytes(b"b")

            first = telemetry.hash_directory_tree(root)
            second = telemetry.hash_directory_tree(root)
            self.assertEqual(first.root, second.root)
            self.assertEqual(first.subdirectories, second.subdirectories)
            self.assertEqual(first.root.file_count, 3)
            self.assertEqual(set(first.subdirectories), {"scraper-a"})
            self.assertEqual(first.subdirectories["scraper-a"].file_count, 2)
            self.assertEqual(first.error_count, 0)
            self.assertEqual(first.hash_basis, "file_content")

            (nested / "b.jpg").write_bytes(b"changed")
            changed = telemetry.hash_directory_tree(root)
            self.assertNotEqual(changed.root.sha256, first.root.sha256)
            self.assertNotEqual(
                changed.subdirectories["scraper-a"].sha256,
                first.subdirectories["scraper-a"].sha256,
            )

    def test_path_size_mode_never_opens_image_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "large.jpg"
            with image.open("wb") as handle:
                handle.truncate(64 * 1024 * 1024)

            with mock.patch.object(
                Path,
                "open",
                side_effect=AssertionError("image content must not be opened"),
            ):
                snapshot = telemetry.hash_directory_tree(
                    root,
                    retain_file_paths={"large.jpg"},
                    hash_file_contents=False,
                )

        self.assertEqual(snapshot.hash_basis, "relative_path_and_size")
        self.assertEqual(snapshot.root.file_count, 1)
        self.assertEqual(snapshot.root.total_bytes, 64 * 1024 * 1024)
        self.assertEqual(set(snapshot.file_hashes), {"large.jpg"})
        payload = telemetry._tree_snapshot_payload(snapshot)
        self.assertEqual(payload["hash_basis"], "relative_path_and_size")
        self.assertEqual(payload["algorithm"], "sha256-merkle-path-size-tree-v1")

    def test_path_size_hash_ignores_content_but_tracks_path_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "image.jpg"
            image.write_bytes(b"first")
            first = telemetry.hash_directory_tree(root, hash_file_contents=False)

            image.write_bytes(b"other")
            same_size = telemetry.hash_directory_tree(root, hash_file_contents=False)
            self.assertEqual(same_size.root.sha256, first.root.sha256)

            image.write_bytes(b"different-size")
            changed_size = telemetry.hash_directory_tree(root, hash_file_contents=False)
            self.assertNotEqual(changed_size.root.sha256, first.root.sha256)

            renamed = root / "renamed.jpg"
            image.rename(renamed)
            changed_path = telemetry.hash_directory_tree(root, hash_file_contents=False)
            self.assertNotEqual(changed_path.root.sha256, changed_size.root.sha256)


class SnapshotCollectionTests(unittest.TestCase):
    def test_image_snapshot_uses_path_and_size_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            images = root / "images"
            images.mkdir()
            (images / "sample.jpg").write_bytes(b"image bytes are not hashed")
            settings = telemetry.TelemetrySettings(
                endpoint="https://example.test/collect",
                auth_token="unit-test-static-bearer-token",
                image_root=images,
                project_root=root,
                timeout_seconds=3,
                match_expiration_seconds=30 * 86400,
            )
            database_snapshot = {
                "lost_artwork_hash": {"sha256": "a" * 64},
                "dependency_hashes": {},
                "counts": {},
                "match_categories": {},
                "scraper_dates": {},
                "matching_programs": [],
                "recent_matches": [],
                "recent_match_total": 0,
            }
            with mock.patch.object(
                telemetry,
                "_collect_database_snapshot",
                return_value=database_snapshot,
            ), mock.patch.object(
                telemetry,
                "_git_identity",
                return_value={},
            ), mock.patch.object(
                telemetry,
                "_runtime_reproducibility_metadata",
                return_value={},
            ), mock.patch.object(
                telemetry,
                "hash_directory_tree",
                wraps=telemetry.hash_directory_tree,
            ) as tree_hash:
                payload = telemetry.collect_telemetry_payload(
                    settings,
                    generated_at=datetime(2026, 8, 16, tzinfo=timezone.utc),
                    trigger="startup",
                )

        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(
            payload["datasets"]["images"]["hash_basis"],
            "relative_path_and_size",
        )
        self.assertFalse(tree_hash.call_args.kwargs["hash_file_contents"])


class PayloadEncodingTests(unittest.TestCase):
    def test_large_match_list_is_truncated_with_a_complete_set_hash(self) -> None:
        records = [
            {
                "id": index,
                "value": hashlib.sha256(f"record-{index}".encode()).hexdigest() * 10,
            }
            for index in range(100)
        ]
        payload = {
            "schema_version": 1,
            "recent_matches": {
                "total_count": len(records),
                "included_count": len(records),
                "truncated": False,
                "records": records,
            },
        }
        encoded = telemetry.encode_bounded_payload(payload, 1_000)
        decoded = json.loads(gzip.decompress(encoded.body))
        recent = decoded["recent_matches"]

        self.assertLessEqual(len(encoded.body), 1_000)
        self.assertTrue(encoded.truncated)
        self.assertLess(encoded.included_match_count, encoded.total_match_count)
        self.assertEqual(recent["included_count"], len(recent["records"]))
        self.assertEqual(recent["omitted_count"], 100 - len(recent["records"]))
        self.assertRegex(recent["candidate_records_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            recent["included_records_sha256"],
            telemetry._canonical_sha256(recent["records"]),
        )

    def test_uncompressed_limit_rejects_highly_compressible_record_growth(self) -> None:
        records = [{"id": index, "value": "a" * 1_000} for index in range(100)]
        payload = {"recent_matches": {"records": records}}
        encoded = telemetry.encode_bounded_payload(
            payload,
            max_bytes=1_000_000,
            max_uncompressed_bytes=10_000,
            max_match_records=100,
        )
        self.assertLessEqual(encoded.uncompressed_bytes, 10_000)
        self.assertTrue(encoded.truncated)

    def test_small_payload_keeps_all_records(self) -> None:
        payload = {"recent_matches": {"records": [{"id": 1}]}}
        encoded = telemetry.encode_bounded_payload(payload, 10_000)
        self.assertFalse(encoded.truncated)
        self.assertEqual(encoded.included_match_count, 1)


class TelemetryDaemonTests(unittest.TestCase):
    def test_daemon_runs_startup_then_daily_workers(self) -> None:
        stop_event = mock.Mock()
        stop_event.is_set.side_effect = [False, False, True]
        startup_worker = mock.Mock()
        startup_worker.poll.return_value = 0
        startup_worker.wait.return_value = 0
        daily_worker = mock.Mock()
        daily_worker.poll.return_value = None
        launch = mock.Mock(side_effect=[startup_worker, daily_worker])
        now = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)

        with mock.patch.object(
            telemetry, "env_bool", return_value=True
        ), mock.patch.object(telemetry, "_stop_worker") as stop_worker:
            self.assertEqual(
                telemetry.run_telemetry_daemon(
                    stop_event,
                    now_fn=lambda: now,
                    launch_worker=launch,
                ),
                0,
            )

        self.assertEqual(
            [call.args[0] for call in launch.call_args_list], ["startup", "daily"]
        )
        stop_worker.assert_called_once_with(daily_worker)

    def test_failed_startup_worker_is_retried_until_it_succeeds(self) -> None:
        stop_event = mock.Mock()
        stop_event.is_set.side_effect = [False, False, False, True]
        failed_worker = mock.Mock()
        failed_worker.poll.return_value = 1
        failed_worker.wait.return_value = 1
        retry_worker = mock.Mock()
        retry_worker.poll.return_value = None
        launch = mock.Mock(side_effect=[failed_worker, retry_worker])

        with mock.patch.object(
            telemetry, "env_bool", return_value=True
        ), mock.patch.object(telemetry, "_stop_worker") as stop_worker, self.assertLogs(
            telemetry.logger, level="ERROR"
        ) as captured_logs:
            self.assertEqual(
                telemetry.run_telemetry_daemon(
                    stop_event,
                    now_fn=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
                    launch_worker=launch,
                ),
                0,
            )

        self.assertIn(
            "Telemetry worker trigger=startup failed exit_code=1",
            "\n".join(captured_logs.output),
        )
        self.assertEqual(
            [call.args[0] for call in launch.call_args_list],
            ["startup", "startup"],
        )
        stop_event.wait.assert_any_call(telemetry.WORKER_LAUNCH_RETRY_SECONDS)
        stop_worker.assert_called_once_with(retry_worker)

    def test_worker_launch_failure_uses_backoff(self) -> None:
        stop_event = mock.Mock()
        stop_event.is_set.side_effect = [False, True]
        launch = mock.Mock(return_value=None)
        with mock.patch.object(telemetry, "env_bool", return_value=True):
            self.assertEqual(
                telemetry.run_telemetry_daemon(
                    stop_event,
                    now_fn=lambda: datetime(2026, 8, 14, tzinfo=timezone.utc),
                    launch_worker=launch,
                ),
                0,
            )
        stop_event.wait.assert_called_once_with(telemetry.WORKER_LAUNCH_RETRY_SECONDS)

    def test_one_shot_exit_code_reports_whether_sync_completed(self) -> None:
        with mock.patch.object(
            telemetry, "try_send_startup_telemetry", return_value="failed"
        ):
            self.assertEqual(telemetry._run_one_shot("startup"), 1)
        with mock.patch.object(
            telemetry, "try_send_startup_telemetry", return_value="sent"
        ):
            self.assertEqual(telemetry._run_one_shot("startup"), 0)

    def test_worker_is_a_separate_process(self) -> None:
        process = mock.Mock()
        with mock.patch.object(
            telemetry.subprocess, "Popen", return_value=process
        ) as popen:
            self.assertIs(telemetry._launch_worker("startup"), process)
        self.assertEqual(
            popen.call_args.args[0],
            (
                telemetry.sys.executable,
                "-m",
                telemetry.TELEMETRY_MODULE,
                "--trigger",
                "startup",
            ),
        )
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


class DailyDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = telemetry.TelemetrySettings(
            endpoint="https://example.test/collect",
            auth_token="unit-test-static-bearer-token",
            image_root=Path("/images"),
            project_root=Path("/project"),
            timeout_seconds=3,
            match_expiration_seconds=30 * 86400,
        )
        self.now = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
        self.encoded = telemetry.EncodedPayload(b"body", "a" * 64, 4, 1, 1, False)
        self.sync_result = telemetry.SyncDeliveryResult(
            sync_id="10000000-0000-4000-8000-000000000001",
            page_count=3,
            total_compressed_bytes=1234,
            operation_sha256="b" * 64,
            last_page=mock.Mock(),
        )

    @mock.patch.object(telemetry, "_record_daily_sync_result", return_value=True)
    @mock.patch.object(telemetry, "deliver_sync_operation")
    @mock.patch.object(telemetry, "collect_telemetry_payload", return_value={})
    @mock.patch.object(telemetry, "_claim_daily_attempt", return_value=True)
    @mock.patch.object(telemetry, "load_telemetry_settings")
    def test_successful_attempt_is_one_claimed_paginated_sync(
        self,
        settings,
        claim,
        collect,
        deliver,
        record,
    ) -> None:
        settings.return_value = self.settings
        deliver.return_value = self.sync_result
        self.assertEqual(telemetry.try_send_daily_telemetry(self.now), "sent")
        claim.assert_called_once_with(date(2026, 8, 14))
        collect.assert_called_once_with(
            self.settings,
            generated_at=self.now,
            trigger="daily",
        )
        deliver.assert_called_once_with(
            self.settings,
            trigger="daily",
            generated_at=self.now,
            summary={},
        )
        record.assert_called_once_with(date(2026, 8, 14), self.sync_result)

    @mock.patch.object(telemetry, "_record_daily_result")
    @mock.patch.object(telemetry, "deliver_sync_operation", side_effect=TimeoutError)
    @mock.patch.object(telemetry, "collect_telemetry_payload", return_value={})
    @mock.patch.object(telemetry, "_claim_daily_attempt", return_value=True)
    @mock.patch.object(telemetry, "load_telemetry_settings")
    def test_failed_sync_is_not_raised_to_the_pipeline(
        self,
        settings,
        _claim,
        _collect,
        _deliver,
        record,
    ) -> None:
        settings.return_value = self.settings
        self.assertEqual(telemetry.try_send_daily_telemetry(self.now), "failed")
        self.assertEqual(record.call_args.kwargs["status"], "failed")
        self.assertEqual(record.call_args.kwargs["error_class"], "TimeoutError")

    @mock.patch.object(telemetry, "deliver_sync_operation")
    @mock.patch.object(telemetry, "collect_telemetry_payload", return_value={})
    @mock.patch.object(telemetry, "_claim_daily_attempt")
    @mock.patch.object(telemetry, "load_telemetry_settings")
    def test_startup_trigger_creates_one_independent_sync(
        self,
        settings,
        claim,
        collect,
        deliver,
    ) -> None:
        settings.return_value = self.settings
        deliver.return_value = self.sync_result
        self.assertEqual(telemetry.try_send_startup_telemetry(self.now), "sent")
        claim.assert_not_called()
        collect.assert_called_once_with(
            self.settings,
            generated_at=self.now,
            trigger="startup",
        )
        deliver.assert_called_once_with(
            self.settings,
            trigger="startup",
            generated_at=self.now,
            summary={},
        )

    @mock.patch.object(telemetry, "load_telemetry_settings")
    @mock.patch.object(telemetry, "_claim_daily_attempt", return_value=False)
    def test_existing_daily_claim_skips_collection(self, claim, settings) -> None:
        settings.return_value = self.settings
        with mock.patch.object(telemetry, "collect_telemetry_payload") as collect:
            self.assertEqual(
                telemetry.try_send_daily_telemetry(self.now), "already_attempted"
            )
        claim.assert_called_once()
        collect.assert_not_called()

    def test_post_uses_gzip_and_idempotency_headers(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.status = 202
        opener = mock.MagicMock()
        opener.open.return_value = response
        with mock.patch.object(telemetry, "build_opener", return_value=opener):
            self.assertEqual(
                telemetry._post_payload(
                    self.settings,
                    self.encoded,
                    idempotency_scope="daily-2026-08-14",
                ),
                202,
            )
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.data, b"body")
        self.assertEqual(request.get_header("Content-encoding"), "gzip")
        self.assertEqual(request.get_header("X-uncompressed-content-length"), "4")
        self.assertEqual(request.get_header("X-compressed-content-length"), "4")
        self.assertEqual(request.get_header("X-uncompressed-content-sha256"), "a" * 64)
        self.assertIn("2026-08-14", request.get_header("Idempotency-key"))

    def test_post_rejects_redirects(self) -> None:
        opener = mock.MagicMock()
        opener.open.side_effect = HTTPError(
            self.settings.endpoint,
            302,
            "Found",
            {},
            None,
        )
        with mock.patch.object(telemetry, "build_opener", return_value=opener):
            with self.assertRaisesRegex(telemetry.TelemetryHttpError, "HTTP 302"):
                telemetry._post_payload(
                    self.settings,
                    self.encoded,
                    idempotency_scope="daily-2026-08-14",
                )


if __name__ == "__main__":
    unittest.main()
