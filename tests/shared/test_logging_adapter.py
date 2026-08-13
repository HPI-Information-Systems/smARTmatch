"""Behavioral coverage for the cross-container logging adapter."""

from __future__ import annotations

import io
import logging
import multiprocessing
import os
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from shared import logging_adapter


def _write_records(log_dir: str, container_name: str, worker: int, count: int) -> None:
    handler = logging_adapter.DailyContainerFileHandler(
        Path(log_dir), container_name, retention_days=30
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        for item in range(count):
            record = logging.LogRecord(
                name="multiprocess-test",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg=f"worker={worker} item={item}",
                args=(),
                exc_info=None,
            )
            handler.handle(record)
    finally:
        handler.close()


def _write_inherited_records(
    handler: logging_adapter.DailyContainerFileHandler,
    worker: int,
    count: int,
) -> None:
    try:
        for item in range(count):
            record = logging.LogRecord(
                name="inherited-handler-test",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg=f"inherited-worker={worker} item={item}",
                args=(),
                exc_info=None,
            )
            handler.handle(record)
    finally:
        handler.close()


class LoggingAdapterTests(unittest.TestCase):
    def tearDown(self) -> None:
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        logging.captureWarnings(False)
        sys.excepthook = sys.__excepthook__
        threading.excepthook = threading.__excepthook__

    def _environment(self, log_dir: str, *, level: str = "ALL") -> dict[str, str]:
        return {
            logging_adapter.LOG_LEVEL_ENV: level,
            logging_adapter.LOG_RETENTION_DAYS_ENV: "30",
            logging_adapter.LOG_DIR_ENV: log_dir,
            logging_adapter.CONTAINER_NAME_ENV: "test-container",
        }

    def test_all_mode_splits_console_and_writes_daily_file(self) -> None:
        with TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, self._environment(directory), clear=True
        ), redirect_stdout(io.StringIO()) as stdout, redirect_stderr(
            io.StringIO()
        ) as stderr:
            settings = logging_adapter.configure_logging()
            logger = logging.getLogger("smartmatch.test")
            logger.debug("debug event")
            logger.error("error event")

            self.assertIn("debug event", stdout.getvalue())
            self.assertNotIn("error event", stdout.getvalue())
            self.assertIn("error event", stderr.getvalue())
            log_path = directory + f"/test-container_{date.today().isoformat()}.txt"
            contents = Path(log_path).read_text()

        self.assertEqual(settings.level, logging_adapter.LogLevel.ALL)
        self.assertIn("debug event", contents)
        self.assertIn("error event", contents)
        self.assertIn("container=test-container", contents)

    def test_stderr_console_mode_preserves_protocol_stdout(self) -> None:
        with TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, self._environment(directory), clear=True
        ), redirect_stdout(io.StringIO()) as stdout, redirect_stderr(
            io.StringIO()
        ) as stderr:
            logging_adapter.configure_logging(console_mode="stderr")
            logging.getLogger("listener").info("listener status")

        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("listener status", stderr.getvalue())

    def test_error_mode_suppresses_lower_levels_everywhere(self) -> None:
        with TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, self._environment(directory, level="ERROR"), clear=True
        ), redirect_stdout(io.StringIO()) as stdout, redirect_stderr(
            io.StringIO()
        ) as stderr:
            logging_adapter.configure_logging()
            logger = logging.getLogger("smartmatch.test")
            logger.warning("hidden warning")
            logger.error("visible error")
            contents = next(Path(directory).glob("test-container_*.txt")).read_text()

        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("hidden warning", stderr.getvalue())
        self.assertIn("visible error", stderr.getvalue())
        self.assertNotIn("hidden warning", contents)
        self.assertIn("visible error", contents)

    def test_uncaught_exceptions_are_written_through_logging(self) -> None:
        with TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, self._environment(directory, level="ERROR"), clear=True
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            logging_adapter.configure_logging()
            error = RuntimeError("unhandled failure")
            sys.excepthook(RuntimeError, error, error.__traceback__)
            contents = next(Path(directory).glob("test-container_*.txt")).read_text()

        self.assertIn("Uncaught exception", contents)
        self.assertIn("RuntimeError: unhandled failure", contents)

    def test_invalid_environment_fails_fast(self) -> None:
        invalid_values = (
            ({logging_adapter.LOG_LEVEL_ENV: "INFO"}, logging_adapter.LOG_LEVEL_ENV),
            (
                {logging_adapter.LOG_RETENTION_DAYS_ENV: "0"},
                logging_adapter.LOG_RETENTION_DAYS_ENV,
            ),
            (
                {logging_adapter.CONTAINER_NAME_ENV: "../escape"},
                logging_adapter.CONTAINER_NAME_ENV,
            ),
        )
        for override, expected in invalid_values:
            environment = self._environment("/tmp")
            environment.update(override)
            with self.subTest(override=override), mock.patch.dict(
                os.environ, environment, clear=True
            ), self.assertRaisesRegex(ValueError, expected):
                logging_adapter.load_logging_settings()

    def test_log_files_are_host_operator_readable(self) -> None:
        with TemporaryDirectory() as directory:
            handler = logging_adapter.DailyContainerFileHandler(
                Path(directory), "frontend", retention_days=30
            )
            path = handler.current_log_path
            handler.close()
            mode = path.stat().st_mode & 0o777

        self.assertEqual(mode, 0o644)

    def test_retention_keeps_exactly_the_configured_calendar_window(self) -> None:
        today = date.today()
        with TemporaryDirectory() as directory:
            log_dir = Path(directory)
            expired = log_dir / f"service_{today - timedelta(days=30)}.txt"
            retained = log_dir / f"service_{today - timedelta(days=29)}.txt"
            unrelated = log_dir / f"other_{today - timedelta(days=90)}.txt"
            for path in (expired, retained, unrelated):
                path.write_text("old\n")

            handler = logging_adapter.DailyContainerFileHandler(
                log_dir, "service", retention_days=30
            )
            handler.close()

            self.assertFalse(expired.exists())
            self.assertTrue(retained.exists())
            self.assertTrue(unrelated.exists())

    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX timezone support")
    def test_local_date_honors_non_utc_system_timezone(self) -> None:
        original_tz = os.environ.get("TZ")
        timestamp = datetime(2024, 1, 1, 12, tzinfo=timezone.utc).timestamp()
        try:
            os.environ["TZ"] = "UTC-14"
            time.tzset()
            self.assertEqual(logging_adapter._local_date(timestamp), date(2024, 1, 2))
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()

    def test_handler_switches_files_at_system_local_date_boundaries(self) -> None:
        today = date.today()
        yesterday = today - timedelta(days=1)
        with TemporaryDirectory() as directory:
            handler = logging_adapter.DailyContainerFileHandler(
                Path(directory), "matching", retention_days=30
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            try:
                for day, message in ((yesterday, "before"), (today, "after")):
                    record = logging.LogRecord(
                        name="rollover-test",
                        level=logging.INFO,
                        pathname=__file__,
                        lineno=1,
                        msg=message,
                        args=(),
                        exc_info=None,
                    )
                    record.created = time.mktime(day.timetuple()) + 12 * 60 * 60
                    handler.handle(record)
            finally:
                handler.close()

            self.assertIn(
                "before",
                (Path(directory) / f"matching_{yesterday}.txt").read_text(),
            )
            self.assertIn(
                "after", (Path(directory) / f"matching_{today}.txt").read_text()
            )

    def test_delayed_record_outside_retention_is_not_discarded(self) -> None:
        today = date.today()
        yesterday = today - timedelta(days=1)
        with TemporaryDirectory() as directory:
            handler = logging_adapter.DailyContainerFileHandler(
                Path(directory), "frontend", retention_days=1
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            try:
                record = logging.LogRecord(
                    name="delayed-record-test",
                    level=logging.ERROR,
                    pathname=__file__,
                    lineno=1,
                    msg="delayed failure",
                    args=(),
                    exc_info=None,
                )
                record.created = time.mktime(yesterday.timetuple()) + 12 * 60 * 60
                handler.handle(record)
            finally:
                handler.close()

            self.assertFalse(
                (Path(directory) / f"frontend_{yesterday}.txt").exists()
            )
            self.assertIn(
                "delayed failure",
                (Path(directory) / f"frontend_{today}.txt").read_text(),
            )

    def test_quiet_service_runs_retention_at_local_midnight(self) -> None:
        today = date.today()
        tomorrow = today + timedelta(days=1)
        with TemporaryDirectory() as directory, mock.patch.object(
            logging_adapter,
            "_seconds_until_next_local_midnight",
            return_value=0.01,
        ):
            handler = logging_adapter.DailyContainerFileHandler(
                Path(directory), "frontend", retention_days=30
            )
            expired = Path(directory) / f"frontend_{today - timedelta(days=29)}.txt"
            expired.write_text("expired\n")
            try:
                with mock.patch.object(
                    logging_adapter, "_local_date", return_value=tomorrow
                ):
                    deadline = time.monotonic() + 1.0
                    while expired.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
            finally:
                handler.close()

            self.assertFalse(expired.exists())
            self.assertTrue((Path(directory) / f"frontend_{tomorrow}.txt").exists())

    def test_daily_handler_is_safe_for_unrelated_processes(self) -> None:
        process_count = 4
        records_per_process = 40
        with TemporaryDirectory() as directory:
            context = multiprocessing.get_context("fork")
            processes = [
                context.Process(
                    target=_write_records,
                    args=(directory, "scrapers", worker, records_per_process),
                )
                for worker in range(process_count)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)

            path = next(Path(directory).glob("scrapers_*.txt"))
            lines = path.read_text().splitlines()

        self.assertEqual(len(lines), process_count * records_per_process)
        self.assertEqual(len(set(lines)), len(lines))

    def test_forked_processes_reopen_inherited_lock_descriptors(self) -> None:
        process_count = 3
        records_per_process = 30
        with TemporaryDirectory() as directory:
            handler = logging_adapter.DailyContainerFileHandler(
                Path(directory), "matching", retention_days=30
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            context = multiprocessing.get_context("fork")
            processes = [
                context.Process(
                    target=_write_inherited_records,
                    args=(handler, worker, records_per_process),
                )
                for worker in range(process_count)
            ]
            try:
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(timeout=10)
                    self.assertEqual(process.exitcode, 0)
            finally:
                handler.close()

            lines = next(Path(directory).glob("matching_*.txt")).read_text().splitlines()

        self.assertEqual(len(lines), process_count * records_per_process)
        self.assertEqual(len(set(lines)), len(lines))

    def test_reconfiguration_does_not_duplicate_handlers(self) -> None:
        with TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, self._environment(directory), clear=True
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            logging_adapter.configure_logging()
            logging_adapter.configure_logging()
            handlers = logging.getLogger().handlers

        self.assertEqual(len(handlers), 3)
        self.assertEqual(
            sum(
                isinstance(item, logging_adapter.DailyContainerFileHandler)
                for item in handlers
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
