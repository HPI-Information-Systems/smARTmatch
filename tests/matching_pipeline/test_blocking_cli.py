"""Offline unit coverage for the blocking command-line interface."""

from __future__ import annotations

import contextlib
import io
import logging
import os
import runpy
import sys
import unittest
import warnings
from pathlib import Path
from unittest import mock

from matching_pipeline.image_blocking import __main__ as cli
from matching_pipeline.image_blocking import pipeline


class CliTests(unittest.TestCase):
    def test_parser_defaults_and_explicit_values(self) -> None:
        with mock.patch.dict(os.environ, {"BLOCKING_LOG_LEVEL": "warning"}):
            defaults = cli.build_parser().parse_args([])
        self.assertEqual(defaults.log_level, "warning")
        self.assertFalse(defaults.only_write_input_csv)

        args = cli.build_parser().parse_args(
            [
                "--input-csv",
                "in.csv",
                "--top-k",
                "2",
                "--image-batch-size",
                "3",
                "--candidate-shard-auction-images",
                "4",
                "--lost-limit",
                "5",
                "--auction-limit",
                "6",
                "--include-processed-auction-images",
                "--dtype",
                "float32",
                "--no-compile",
                "--hf-token",
                "secret",
                "--clear-candidates",
                "--log-level",
                "debug",
            ]
        )
        self.assertEqual(args.input_csv, Path("in.csv"))
        self.assertEqual(args.top_k, 2)
        self.assertTrue(args.include_processed_auction_images)
        self.assertTrue(args.no_compile)
        self.assertTrue(args.clear_candidates)

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["--dtype", "bad"])

    def test_logging_configuration_valid_and_invalid(self) -> None:
        with mock.patch.object(logging, "basicConfig") as basic:
            cli._configure_logging("debug")
        self.assertEqual(basic.call_args.kwargs["level"], logging.DEBUG)
        self.assertTrue(basic.call_args.kwargs["force"])
        with self.assertRaisesRegex(ValueError, "Invalid log level"):
            cli._configure_logging("not-a-level")

    def test_cli_invalid_log_and_conflicting_csv_options_use_parser_errors(self) -> None:
        for argv, message in (
            (["blocking", "--log-level", "bad"], "Invalid log level"),
            (
                ["blocking", "--only-write-input-csv", "--input-csv", "x.csv"],
                "cannot be used",
            ),
        ):
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", argv), mock.patch.object(
                cli, "_configure_logging"
            ) as configure, contextlib.redirect_stderr(io.StringIO()) as stderr:
                if "bad" in argv:
                    configure.side_effect = ValueError("Invalid log level: 'bad'")
                with self.assertRaises(SystemExit) as raised:
                    cli.parse_blocking_args_and_run_blocking_with_result()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, stderr.getvalue())

    def test_only_write_input_csv_prints_result(self) -> None:
        csv_result = pipeline.BlockingInputCsvResult(Path("input.csv"), 2, 3)
        argv = [
            "blocking",
            "--only-write-input-csv",
            "--lost-limit",
            "4",
            "--auction-limit",
            "5",
            "--include-processed-auction-images",
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            cli, "_configure_logging"
        ), mock.patch.object(
            cli, "create_blocking_input_csv", return_value=csv_result
        ) as create, contextlib.redirect_stdout(io.StringIO()) as stdout:
            result = cli.parse_blocking_args_and_run_blocking_with_result(full_pipeline=True)
        self.assertEqual(result, cli.BlockingCliResult(0))
        create.assert_called_once_with(
            lost_limit=4,
            auction_limit=5,
            include_processed_auction_images=True,
        )
        self.assertIn("Input CSV: input.csv", stdout.getvalue())
        self.assertIn("Auction images: 3", stdout.getvalue())

    def test_normal_cli_forwards_kwargs_prints_rows_and_wrapper_exit_code(self) -> None:
        blocking_result = pipeline.BlockingRunResult(
            Path("cache"), 2, 1, 2, 1, 1, 2, 3
        )
        rankings = [
            {
                "auction_file_id": "auction-long",
                "match_candidates": [
                    {"lost_file_id": "lost-1"},
                    {"lost_file_id": "lost-2"},
                ],
            }
        ]
        argv = ["blocking", "--input-csv", "input.csv", "--top-k", "2"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            cli, "_configure_logging"
        ), mock.patch.object(
            cli, "run_image_blocking", return_value=blocking_result
        ) as run, mock.patch.object(
            cli, "load_auction_to_lost_rankings_with_paths", return_value=rankings
        ), contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(cli.parse_blocking_args_and_run_blocking(), 0)

        self.assertEqual(run.call_args.kwargs["input_csv"], Path("input.csv"))
        self.assertEqual(run.call_args.kwargs["top_k"], 2)
        self.assertNotIn("log_level", run.call_args.kwargs)
        output = stdout.getvalue()
        self.assertIn("Blocking cache: cache", output)
        self.assertIn("First candidate rankings", output)
        self.assertIn("auction-long", output)
        self.assertIn("lost-2", output)

    def test_cli_propagates_pipeline_failure(self) -> None:
        with mock.patch.object(sys, "argv", ["blocking"]), mock.patch.object(
            cli, "_configure_logging"
        ), mock.patch.object(
            cli, "run_image_blocking", side_effect=RuntimeError("pipeline failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "pipeline failed"):
                cli.parse_blocking_args_and_run_blocking_with_result()

    def test_ranking_preview_empty_limited_and_exhausted(self) -> None:
        with mock.patch.object(
            cli, "load_auction_to_lost_rankings_with_paths", return_value=[]
        ), contextlib.redirect_stdout(io.StringIO()) as stdout:
            cli._print_ranking_preview()
        self.assertIn("no candidate rankings", stdout.getvalue())

        rankings = [
            {
                "auction_file_id": "a1",
                "match_candidates": [
                    {"lost_file_id": "l1"},
                    {"lost_file_id": "l2"},
                ],
            },
            {"auction_file_id": "a2", "match_candidates": []},
        ]
        with mock.patch.object(
            cli, "load_auction_to_lost_rankings_with_paths", return_value=rankings
        ):
            self.assertEqual(
                cli._ranking_preview_rows(1), [("1", "a1", "1", "l1")]
            )
            self.assertEqual(
                cli._ranking_preview_rows(5),
                [("1", "a1", "1", "l1"), ("2", "a1", "2", "l2")],
            )

    def test_print_table_with_empty_and_wider_rows(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            cli._print_table(("h", "header"), [])
            cli._print_table(("h", "header"), [("long", "x")])
        output = stdout.getvalue()
        self.assertIn("h | header", output)
        self.assertIn("long | x", output)

    def test_module_guard_exits_through_entrypoint(self) -> None:
        result = pipeline.BlockingRunResult(Path("cache"), 0, 0, 0, 0, 0, 0)
        with mock.patch.object(sys, "argv", ["blocking"]), mock.patch.object(
            pipeline, "run_image_blocking", return_value=result
        ), mock.patch(
            "matching_pipeline.shared.artifacts.load_auction_to_lost_rankings_with_paths",
            return_value=[],
        ), mock.patch("logging.basicConfig"), contextlib.redirect_stdout(
            io.StringIO()
        ), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with self.assertRaises(SystemExit) as raised:
                runpy.run_module(
                    "matching_pipeline.image_blocking.__main__", run_name="__main__"
                )
        self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
