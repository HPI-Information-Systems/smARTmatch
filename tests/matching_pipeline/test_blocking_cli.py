"""Offline unit coverage for the blocking command-line interface."""

from __future__ import annotations

import contextlib
import io
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
        defaults = cli.build_parser().parse_args([])
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
            ]
        )
        self.assertEqual(args.input_csv, Path("in.csv"))
        self.assertEqual(args.top_k, 2)
        self.assertTrue(args.include_processed_auction_images)
        self.assertTrue(args.no_compile)
        self.assertTrue(args.clear_candidates)

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["--dtype", "bad"])

    def test_cli_rejects_removed_log_flag_and_conflicting_csv_options(self) -> None:
        for argv, message in (
            (["blocking", "--log-level", "bad"], "unrecognized arguments"),
            (
                ["blocking", "--only-write-input-csv", "--input-csv", "x.csv"],
                "cannot be used",
            ),
        ):
            with self.subTest(argv=argv), mock.patch.object(
                sys, "argv", argv
            ), mock.patch.object(cli, "configure_logging"), contextlib.redirect_stderr(
                io.StringIO()
            ) as stderr:
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
            cli, "configure_logging"
        ) as configure, mock.patch.object(
            cli, "create_blocking_input_csv", return_value=csv_result
        ) as create, self.assertLogs(cli.logger, level="INFO") as captured:
            result = cli.parse_blocking_args_and_run_blocking_with_result(full_pipeline=True)
        self.assertEqual(result, cli.BlockingCliResult(0))
        create.assert_called_once_with(
            lost_limit=4,
            auction_limit=5,
            include_processed_auction_images=True,
        )
        configure.assert_called_once_with()
        output = "\n".join(captured.output)
        self.assertIn("Input CSV: input.csv", output)
        self.assertIn("Auction images: 3", output)

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
            cli, "configure_logging"
        ), mock.patch.object(
            cli, "run_image_blocking", return_value=blocking_result
        ) as run, mock.patch.object(
            cli, "load_auction_to_lost_rankings_with_paths", return_value=rankings
        ), self.assertLogs(cli.logger, level="INFO") as captured:
            self.assertEqual(cli.parse_blocking_args_and_run_blocking(), 0)

        self.assertEqual(run.call_args.kwargs["input_csv"], Path("input.csv"))
        self.assertEqual(run.call_args.kwargs["top_k"], 2)
        self.assertNotIn("log_level", run.call_args.kwargs)
        output = "\n".join(captured.output)
        self.assertIn("Blocking cache: cache", output)
        self.assertIn("First candidate rankings", output)
        self.assertIn("auction-long", output)
        self.assertIn("lost-2", output)

    def test_cli_propagates_pipeline_failure(self) -> None:
        with mock.patch.object(sys, "argv", ["blocking"]), mock.patch.object(
            cli, "configure_logging"
        ), mock.patch.object(
            cli, "run_image_blocking", side_effect=RuntimeError("pipeline failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "pipeline failed"):
                cli.parse_blocking_args_and_run_blocking_with_result()

    def test_ranking_preview_empty_limited_and_exhausted(self) -> None:
        with mock.patch.object(
            cli, "load_auction_to_lost_rankings_with_paths", return_value=[]
        ), self.assertLogs(cli.logger, level="INFO") as captured:
            cli._print_ranking_preview()
        self.assertIn("no candidate rankings", "\n".join(captured.output))

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
        with self.assertLogs(cli.logger, level="INFO") as captured:
            cli._print_table(("h", "header"), [])
            cli._print_table(("h", "header"), [("long", "x")])
        output = "\n".join(captured.output)
        self.assertIn("h | header", output)
        self.assertIn("long | x", output)

    def test_module_guard_exits_through_entrypoint(self) -> None:
        result = pipeline.BlockingRunResult(Path("cache"), 0, 0, 0, 0, 0, 0)
        with mock.patch.object(sys, "argv", ["blocking"]), mock.patch.object(
            pipeline, "run_image_blocking", return_value=result
        ), mock.patch(
            "matching_pipeline.shared.artifacts.load_auction_to_lost_rankings_with_paths",
            return_value=[],
        ), mock.patch(
            "shared.logging_adapter.configure_logging"
        ), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            with self.assertRaises(SystemExit) as raised:
                runpy.run_module(
                    "matching_pipeline.image_blocking.__main__", run_name="__main__"
                )
        self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
