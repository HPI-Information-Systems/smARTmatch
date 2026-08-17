"""Offline unit tests for shared ranking artifacts."""

from __future__ import annotations

import hashlib
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq

from matching_pipeline.shared.artifacts import rankings
from tests.matching_pipeline._shared_test_support import TemporaryPipelineTest


class RankingArtifactTests(TemporaryPipelineTest):
    def test_write_schema_and_input_validation(self) -> None:
        auction_digest = hashlib.sha256(b"auction").hexdigest()
        lost_digest = hashlib.sha256(b"lost").hexdigest()
        output = rankings.write_auction_to_lost_rankings_parquet(
            "part-000000.parquet",
            auction_file_ids=[" a "],
            auction_content_versions=[2],
            auction_content_sha256=[auction_digest],
            lost_file_ids=[8],
            lost_content_versions=[3],
            lost_content_sha256=[lost_digest],
            ranks=["2"],
            blocking_scores=["0.25"],
        )
        table = pq.read_table(output)
        self.assertEqual(
            table.schema,
            pa.schema(
                [
                    ("auction_file_id", pa.string()),
                    ("auction_content_version", pa.int64()),
                    ("auction_content_sha256", pa.string()),
                    ("lost_file_id", pa.string()),
                    ("lost_content_version", pa.int64()),
                    ("lost_content_sha256", pa.string()),
                    ("rank", pa.int16()),
                    ("blocking_score", pa.float32()),
                ]
            ),
        )
        self.assertEqual(table.column("auction_file_id").to_pylist(), ["a"])

        with self.assertRaisesRegex(ValueError, "different lengths"):
            rankings.write_auction_to_lost_rankings_parquet(
                "part-bad.parquet",
                auction_file_ids=["a"],
                auction_content_versions=[1],
                auction_content_sha256=[auction_digest],
                lost_file_ids=[],
                lost_content_versions=[],
                lost_content_sha256=[],
                ranks=[1],
                blocking_scores=[0.5],
            )
        for name in ("", "nested/part.parquet", "../part.parquet"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "must be a file name"):
                    rankings._ranking_part_path(name)

    def test_scalar_ranking_validation(self) -> None:
        self.assertEqual(rankings._required_text(9, "field"), "9")
        for value, message in ((None, "Missing field"), (" ", "Empty field")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    rankings._required_text(value, "field")

        self.assertEqual(rankings._coerce_content_version("2", "version"), 2)
        self.assertIsNone(rankings._coerce_content_version(None, "version"))
        for value in ("bad", 0):
            with self.subTest(content_version=value):
                with self.assertRaisesRegex(ValueError, "version"):
                    rankings._coerce_content_version(value, "version")
        digest = hashlib.sha256(b"image").hexdigest()
        self.assertEqual(
            rankings._coerce_content_sha256(digest.upper(), "digest"),
            digest,
        )
        with self.assertRaisesRegex(ValueError, "64 hexadecimal"):
            rankings._coerce_content_sha256("bad", "digest")

        self.assertEqual(rankings._coerce_rank("1"), 1)
        self.assertEqual(rankings._coerce_rank(32767), 32767)
        for value, message in (
            (None, "Invalid ranking rank"),
            ("bad", "Invalid ranking rank"),
            (0, "must be in"),
            (32768, "must be in"),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, message):
                    rankings._coerce_rank(value)

        self.assertEqual(rankings._coerce_score("0.5"), 0.5)
        for value in (None, "bad"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Invalid blocking_score"):
                    rankings._coerce_score(value)

    def test_summary_missing_empty_complete_and_malformed(self) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "directory not found"):
            rankings.summarize_auction_to_lost_rankings()
        ranking_dir = self.ranking_dir()
        self.assertEqual(
            rankings.summarize_auction_to_lost_rankings(),
            {"part_count": 0, "row_count": 0, "auction_file_count": 0},
        )
        self.write_raw_ranking(
            auction_file_id=["a1", "a2"],
            lost_file_id=["l1", "l2"],
            rank=[1, 1],
            blocking_score=[0.9, 0.8],
        )
        self.write_raw_ranking(
            "part-000001.parquet",
            auction_file_id=["a1"],
            lost_file_id=["l3"],
            rank=[2],
            blocking_score=[0.7],
        )
        self.assertEqual(
            rankings.summarize_auction_to_lost_rankings(),
            {"part_count": 2, "row_count": 3, "auction_file_count": 2},
        )

        for child in ranking_dir.iterdir():
            child.unlink()
        malformed = self.write_raw_ranking(
            auction_file_id=["a"], rank=[1], blocking_score=[0.5]
        )
        with self.assertRaisesRegex(ValueError, "missing columns: lost_file_id"):
            rankings.summarize_auction_to_lost_rankings()
        malformed.unlink()
        self.write_raw_ranking(
            auction_file_id=[None],
            lost_file_id=["l"],
            rank=[1],
            blocking_score=[0.5],
        )
        with self.assertRaisesRegex(ValueError, "Missing auction_file_id"):
            rankings.summarize_auction_to_lost_rankings()

    def test_load_groups_sorts_batches_and_handles_empty(self) -> None:
        ranking_dir = self.ranking_dir()
        self.assertEqual(list(rankings.load_auction_to_lost_rankings_with_paths()), [])
        with self.assertRaisesRegex(ValueError, "batch_size must be positive"):
            list(rankings.load_auction_to_lost_rankings_with_paths(batch_size=0))

        self.write_raw_ranking(
            auction_file_id=["a1", "a1", "a2", "a3"],
            lost_file_id=["l2", "l1", "l1", "l3"],
            rank=[2, 1, 1, 1],
            blocking_score=[0.2, 0.9, 0.8, 0.7],
        )
        path_maps = [
            {"a1": "/a1", "a2": "/a2", "a3": "/a3"},
            {"l1": "/l1", "l2": "/l2", "l3": "/l3"},
        ]
        with mock.patch.object(
            rankings, "read_image_files_parquet", side_effect=path_maps
        ) as read_paths:
            loaded = list(
                rankings.load_auction_to_lost_rankings_with_paths(batch_size=2)
            )
        self.assertEqual(
            [item["auction_file_id"] for item in loaded], ["a1", "a2", "a3"]
        )
        self.assertEqual(
            [item["lost_file_id"] for item in loaded[0]["match_candidates"]],
            ["l1", "l2"],
        )
        read_paths.assert_has_calls([mock.call("auction"), mock.call("lost")])
        with mock.patch.object(
            rankings, "read_image_files_parquet", side_effect=path_maps
        ):
            exact_batch = list(
                rankings.load_auction_to_lost_rankings_with_paths(batch_size=3)
            )
        self.assertEqual(len(exact_batch), 3)
        self.assertEqual(list(rankings._iter_rankings([], {}, {})), [])
        self.assertTrue(ranking_dir.is_dir())

    def test_noncontiguous_and_unknown_path_references(self) -> None:
        path = self.write_raw_ranking(
            auction_file_id=["a1", "a2", "a1"],
            lost_file_id=["l1", "l1", "l1"],
            rank=[1, 1, 2],
            blocking_score=[0.9, 0.8, 0.7],
        )
        with self.assertRaisesRegex(ValueError, "not contiguous"):
            list(
                rankings._iter_rankings(
                    [path], {"a1": "/a1", "a2": "/a2"}, {"l1": "/l1"}
                )
            )

        for auction_paths, lost_paths, message in (
            ({}, {"l1": "/l1"}, "unknown auction file_id"),
            ({"a1": "/a1"}, {}, "unknown lost file_id"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    rankings._make_item(
                        "a1", [(1, "l1", 0.5)], auction_paths, lost_paths
                    )

    def test_ranking_row_schema_and_value_validation(self) -> None:
        path = self.write_raw_ranking(
            auction_file_id=["a"],
            lost_file_id=["l"],
            rank=[1],
            blocking_score=[0.5],
            extra=["ignored"],
        )
        self.assertEqual(
            list(rankings._iter_ranking_rows(pq, [path])), [("a", "l", 1, 0.5)]
        )

        cases = [
            (
                {
                    "auction_file_id": [None],
                    "lost_file_id": ["l"],
                    "rank": [1],
                    "blocking_score": [0.5],
                },
                "Missing auction_file_id",
            ),
            (
                {
                    "auction_file_id": ["a"],
                    "lost_file_id": [" "],
                    "rank": [1],
                    "blocking_score": [0.5],
                },
                "Empty lost_file_id",
            ),
            (
                {
                    "auction_file_id": ["a"],
                    "lost_file_id": ["l"],
                    "rank": ["bad"],
                    "blocking_score": [0.5],
                },
                "Invalid ranking rank",
            ),
            (
                {
                    "auction_file_id": ["a"],
                    "lost_file_id": ["l"],
                    "rank": [1],
                    "blocking_score": ["bad"],
                },
                "Invalid blocking_score",
            ),
        ]
        for columns, message in cases:
            with self.subTest(message=message):
                pq.write_table(pa.table(columns), path)
                with self.assertRaisesRegex(ValueError, message):
                    list(rankings._iter_ranking_rows(pq, [path]))
