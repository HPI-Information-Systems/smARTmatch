"""Offline orchestration tests for the LightGlue matching runner."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from matching_pipeline.image_matching import run_image_matching as runtime


def _summary(rows=1, auctions=1):
    return {
        "part_count": 1 if rows else 0,
        "row_count": rows,
        "auction_file_count": auctions,
    }


def _candidate(file_id, path, score):
    return {
        "lost_file_id": file_id,
        "lost_file_path": path,
        "blocking_score": score,
    }


def _prepare(path, *, resize):
    return mock.Mock(path=Path(path), resize=resize)


class MatchingRuntimeTests(unittest.TestCase):
    def test_empty_artifacts_skip_models(self) -> None:
        cases = [(_summary(0, 1), Path("unused.csv")), (_summary(1, 0), None)]
        for summary, output in cases:
            with (
                self.subTest(summary=summary),
                mock.patch.object(
                    runtime, "_candidate_artifact_summary", return_value=summary
                ),
                mock.patch.object(runtime, "FeatureExtractor") as extractor,
                mock.patch.object(runtime, "env_cache_dir", return_value=Path("cache")),
                self.assertLogs(runtime.logger, level="INFO") as captured,
            ):
                result = runtime.run_image_matching(results_csv=output)
            extractor.assert_not_called()
            self.assertIn("No candidate rows found", "\n".join(captured.output))
            self.assertEqual(result.processed_auction_file_ids, [])
            self.assertEqual(result.accepted_matches, [])
            self.assertEqual(result.pairs_processed, 0)

    def test_invalid_feature_directory_type_is_rejected(self) -> None:
        with self.assertRaises(AssertionError):
            runtime.run_image_matching(feats_dir="not-a-path")

    def test_missing_content_identity_fails_before_model_initialization(self) -> None:
        legacy_item = {
            "auction_file_id": "legacy",
            "auction_file_path": "legacy.jpg",
            "match_candidates": [],
        }
        with (
            mock.patch.object(
                runtime, "_candidate_artifact_summary", return_value=_summary()
            ),
            mock.patch.object(
                runtime,
                "load_auction_to_lost_rankings_with_paths",
                return_value=iter([legacy_item]),
            ),
            mock.patch.object(runtime, "FeatureExtractor") as extractor,
            self.assertRaisesRegex(ValueError, "lost-image content revision"),
        ):
            runtime.run_image_matching(feats_dir=None)
        extractor.assert_not_called()

    def test_resize_worker_config_is_validated_before_model_initialization(
        self,
    ) -> None:
        item = {
            "auction_file_id": "auction",
            "auction_file_path": "auction.jpg",
            "auction_content_version": 2,
            "lost_content_revision": 7,
            "match_candidates": [],
        }
        with (
            mock.patch.object(
                runtime, "_candidate_artifact_summary", return_value=_summary()
            ),
            mock.patch.object(
                runtime,
                "load_auction_to_lost_rankings_with_paths",
                return_value=iter([item]),
            ),
            mock.patch.object(
                runtime,
                "matching_image_resize_workers_from_env",
                side_effect=ValueError("workers must be positive"),
            ),
            mock.patch.object(runtime, "FeatureExtractor") as extractor,
            self.assertRaisesRegex(ValueError, "workers must be positive"),
        ):
            runtime.run_image_matching(feats_dir=None)

        extractor.assert_not_called()

    def test_resize_executor_is_shutdown_when_matching_aborts(self) -> None:
        item = {
            "auction_file_id": "auction",
            "auction_file_path": "auction.jpg",
            "auction_content_version": 2,
            "lost_content_revision": 7,
            "match_candidates": [],
        }
        executor = mock.Mock()
        with (
            mock.patch.object(
                runtime, "_candidate_artifact_summary", return_value=_summary()
            ),
            mock.patch.object(
                runtime,
                "load_auction_to_lost_rankings_with_paths",
                return_value=iter([item]),
            ),
            mock.patch.object(
                runtime, "matching_image_resize_workers_from_env", return_value=2
            ),
            mock.patch.object(runtime, "FeatureExtractor"),
            mock.patch.object(runtime, "FeatureMatcher"),
            mock.patch.object(runtime, "MatchClassifier"),
            mock.patch.object(runtime, "ThreadPoolExecutor", return_value=executor),
            mock.patch.object(
                runtime,
                "_MatchingProgress",
                side_effect=KeyboardInterrupt,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            runtime.run_image_matching(feats_dir=None)

        executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)

    def test_success_rejection_pair_failure_and_image_failure(self) -> None:
        extractor = mock.Mock(device="cpu")
        matcher = mock.Mock(device="cpu")
        classifier = mock.Mock()
        auction_features = {"keypoints": [[1, 2]]}
        lost_features = {"keypoints": [[3, 4]]}

        def extract(prepared):
            if prepared.path.name == "auction-bad.jpg":
                raise RuntimeError("unreadable auction")
            return auction_features

        def load_or_extract(
            path,
            image_path,
            save_missing_feats=True,
            prepared_image=None,
        ):
            if image_path.name == "lost-bad.jpg":
                raise RuntimeError("unreadable pair")
            return lost_features

        extractor.extract_prepared.side_effect = extract
        extractor.load_or_extract.side_effect = load_or_extract
        matcher.match.side_effect = [{"scores": [0.9]}, {"scores": [0.1]}]
        classifier.classify_matches.side_effect = [(True, 0.8), (False, 0.2)]
        items = [
            {
                "auction_file_id": "auction-1",
                "auction_file_path": "auction.jpg",
                "auction_content_version": 2,
                "lost_content_revision": 7,
                "match_candidates": [
                    _candidate("accepted", "lost-ok.jpg", "0.7"),
                    _candidate("rejected", "lost-no.jpg", 0.4),
                    _candidate("failed", "lost-bad.jpg", 0.3),
                ],
            },
            {
                "auction_file_id": "auction-2",
                "auction_file_path": "auction-bad.jpg",
                "auction_content_version": 3,
                "lost_content_revision": 7,
                "match_candidates": [_candidate("unused", "unused.jpg", 0.1)],
            },
        ]
        output = Path("relative-results.csv")
        cache = Path("relative-cache")
        payload = {"match_count": 1}

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    runtime,
                    "_candidate_artifact_summary",
                    return_value=_summary(4, 2),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    runtime,
                    "load_auction_to_lost_rankings_with_paths",
                    return_value=iter(items),
                )
            )
            stack.enter_context(
                mock.patch.object(runtime, "FeatureExtractor", return_value=extractor)
            )
            stack.enter_context(
                mock.patch.object(runtime, "prepare_image", side_effect=_prepare)
            )
            stack.enter_context(
                mock.patch.object(runtime, "FeatureMatcher", return_value=matcher)
            )
            stack.enter_context(
                mock.patch.object(runtime, "MatchClassifier", return_value=classifier)
            )
            build = stack.enter_context(
                mock.patch.object(
                    runtime,
                    "build_keypoint_match_visualization",
                    return_value=payload,
                )
            )
            write_results = stack.enter_context(
                mock.patch.object(runtime, "_write_results")
            )
            captured = stack.enter_context(
                self.assertLogs(runtime.logger, level="INFO")
            )
            result = runtime.run_image_matching(
                results_csv=output,
                feats_dir=cache,
                save_missing_feats=False,
            )

        self.assertEqual(result.processed_auction_file_ids, [])
        self.assertEqual(result.lost_content_revision, 7)
        self.assertEqual(
            result.auction_content_versions,
            {"auction-1": 2, "auction-2": 3},
        )
        self.assertEqual(
            (result.pairs_processed, result.failed_images, result.failed_pairs),
            (3, 1, 1),
        )
        self.assertEqual(len(result.accepted_matches), 1)
        accepted = result.accepted_matches[0]
        self.assertEqual(
            (accepted.auction_file_id, accepted.lost_file_id),
            ("auction-1", "accepted"),
        )
        self.assertEqual((accepted.confidence, accepted.blocking_score), (0.8, 0.7))
        self.assertIs(accepted.keypoint_matches, payload)
        self.assertEqual(extractor.load_or_extract.call_count, 3)
        first_call = extractor.load_or_extract.call_args_list[0]
        self.assertEqual(first_call.args[0], cache / "accepted.pt")
        self.assertFalse(first_call.kwargs["save_missing_feats"])
        build.assert_called_once()
        write_results.assert_called_once_with(output, result.accepted_matches)
        self.assertIn("Found 1 accepted matches", "\n".join(captured.output))

    def test_cache_disabled_extracts_lost_image(self) -> None:
        extractor = mock.Mock(device="cpu")
        matcher = mock.Mock(device="cpu", match=mock.Mock(return_value={"scores": []}))
        classifier = mock.Mock()
        classifier.classify_matches.return_value = (False, 0.25)
        extractor.extract_prepared.side_effect = [
            {"auction": 1},
            {"lost": 1},
            {"empty": 1},
        ]
        items = [
            {
                "auction_file_id": "one",
                "auction_file_path": "auction.jpg",
                "auction_content_version": 2,
                "lost_content_revision": 7,
                "match_candidates": [_candidate("lost", "lost.jpg", 0.1)],
            },
            {
                "auction_file_id": "empty",
                "auction_file_path": "empty.jpg",
                "auction_content_version": 3,
                "lost_content_revision": 7,
                "match_candidates": [],
            },
        ]
        with (
            mock.patch.object(
                runtime, "_candidate_artifact_summary", return_value=_summary(1, 2)
            ),
            mock.patch.object(
                runtime,
                "load_auction_to_lost_rankings_with_paths",
                return_value=iter(items),
            ),
            mock.patch.object(runtime, "FeatureExtractor", return_value=extractor),
            mock.patch.object(runtime, "FeatureMatcher", return_value=matcher),
            mock.patch.object(runtime, "MatchClassifier", return_value=classifier),
            mock.patch.object(runtime, "prepare_image", side_effect=_prepare),
            mock.patch.object(runtime, "_write_results") as write_results,
        ):
            result = runtime.run_image_matching(feats_dir=None)

        self.assertEqual(result.pairs_processed, 1)
        self.assertEqual(result.accepted_matches, [])
        self.assertEqual(result.processed_auction_file_ids, ["one", "empty"])
        extractor.load_or_extract.assert_not_called()
        self.assertEqual(extractor.extract_prepared.call_count, 3)
        write_results.assert_called_once_with(None, [])

    def test_valid_feature_cache_skips_candidate_cpu_preparation(self) -> None:
        extractor = mock.Mock(device="cpu")
        extractor.has_compatible_feature_cache.return_value = True
        extractor.extract_prepared.return_value = {"auction": 1}
        extractor.load_or_extract.return_value = {"lost": 1}
        matcher = mock.Mock(
            device="cpu",
            match=mock.Mock(return_value={"scores": []}),
        )
        classifier = mock.Mock()
        classifier.classify_matches.return_value = (False, 0.2)
        item = {
            "auction_file_id": "auction",
            "auction_file_path": "auction.jpg",
            "auction_content_version": 2,
            "lost_content_revision": 7,
            "match_candidates": [_candidate("lost", "lost.jpg", 0.1)],
        }
        with (
            mock.patch.object(
                runtime, "_candidate_artifact_summary", return_value=_summary()
            ),
            mock.patch.object(
                runtime,
                "load_auction_to_lost_rankings_with_paths",
                return_value=iter([item]),
            ),
            mock.patch.object(runtime, "FeatureExtractor", return_value=extractor),
            mock.patch.object(runtime, "FeatureMatcher", return_value=matcher),
            mock.patch.object(runtime, "MatchClassifier", return_value=classifier),
            mock.patch.object(
                runtime, "prepare_image", side_effect=_prepare
            ) as prepare,
            mock.patch.object(runtime, "_write_results"),
        ):
            result = runtime.run_image_matching(feats_dir=Path("cache"))

        self.assertEqual(result.pairs_processed, 1)
        prepare.assert_called_once_with(
            Path("auction.jpg"),
            resize=runtime.DEFAULT_IMAGE_RESIZE,
        )
        self.assertIsNone(extractor.load_or_extract.call_args.kwargs["prepared_image"])

    def test_auction_cuda_oom_aborts_stage_and_logs_memory(self) -> None:
        extractor = mock.Mock(device="cuda:0")
        extractor.extract_prepared.side_effect = runtime.torch.cuda.OutOfMemoryError(
            "auction oom"
        )
        matcher = mock.Mock(device="cuda:0")
        item = {
            "auction_file_id": "auction",
            "auction_file_path": "auction.jpg",
            "auction_content_version": 2,
            "lost_content_revision": 7,
            "match_candidates": [_candidate("lost", "lost.jpg", 0.1)],
        }
        with (
            mock.patch.object(
                runtime, "_candidate_artifact_summary", return_value=_summary()
            ),
            mock.patch.object(
                runtime,
                "load_auction_to_lost_rankings_with_paths",
                return_value=iter([item]),
            ),
            mock.patch.object(runtime, "FeatureExtractor", return_value=extractor),
            mock.patch.object(runtime, "FeatureMatcher", return_value=matcher),
            mock.patch.object(runtime, "MatchClassifier"),
            mock.patch.object(runtime, "prepare_image", side_effect=_prepare),
            mock.patch.object(runtime, "log_cuda_memory_best_effort") as memory_log,
            mock.patch.object(runtime, "_write_results") as write_results,
            self.assertLogs(runtime.logger, level="ERROR") as captured,
            self.assertRaises(runtime.torch.cuda.OutOfMemoryError),
        ):
            runtime.run_image_matching(feats_dir=None)

        memory_log.assert_called_once()
        write_results.assert_not_called()
        self.assertIn(
            "CUDA OOM; aborting image-matching stage",
            "\n".join(captured.output),
        )

    def test_pair_cuda_oom_aborts_stage_instead_of_trying_next_pair(self) -> None:
        extractor = mock.Mock(device="cuda:0")
        extractor.extract_prepared.return_value = {"features": 1}
        matcher = mock.Mock(device="cuda:0")
        matcher.match.side_effect = runtime.torch.cuda.OutOfMemoryError("pair oom")
        item = {
            "auction_file_id": "auction",
            "auction_file_path": "auction.jpg",
            "auction_content_version": 2,
            "lost_content_revision": 7,
            "match_candidates": [
                _candidate("first", "first.jpg", 0.2),
                _candidate("second", "second.jpg", 0.1),
            ],
        }
        with (
            mock.patch.object(
                runtime, "_candidate_artifact_summary", return_value=_summary(2, 1)
            ),
            mock.patch.object(
                runtime,
                "load_auction_to_lost_rankings_with_paths",
                return_value=iter([item]),
            ),
            mock.patch.object(runtime, "FeatureExtractor", return_value=extractor),
            mock.patch.object(runtime, "FeatureMatcher", return_value=matcher),
            mock.patch.object(runtime, "MatchClassifier"),
            mock.patch.object(runtime, "prepare_image", side_effect=_prepare),
            mock.patch.object(runtime, "log_cuda_memory_best_effort") as memory_log,
            mock.patch.object(runtime, "_write_results") as write_results,
            self.assertRaises(runtime.torch.cuda.OutOfMemoryError),
        ):
            runtime.run_image_matching(feats_dir=None)

        self.assertEqual(matcher.match.call_count, 1)
        memory_log.assert_called_once()
        write_results.assert_not_called()

    def test_only_empty_group_reports_no_average(self) -> None:
        extractor = mock.Mock(device="cpu")
        matcher = mock.Mock(device="cpu")
        item = {
            "auction_file_id": "empty",
            "auction_file_path": "empty.jpg",
            "auction_content_version": 2,
            "lost_content_revision": 7,
            "match_candidates": [],
        }
        with (
            mock.patch.object(
                runtime, "_candidate_artifact_summary", return_value=_summary()
            ),
            mock.patch.object(
                runtime,
                "load_auction_to_lost_rankings_with_paths",
                return_value=iter([item]),
            ),
            mock.patch.object(runtime, "FeatureExtractor", return_value=extractor),
            mock.patch.object(runtime, "FeatureMatcher", return_value=matcher),
            mock.patch.object(runtime, "MatchClassifier"),
            mock.patch.object(runtime, "prepare_image", side_effect=_prepare),
            mock.patch.object(runtime, "_write_results"),
        ):
            result = runtime.run_image_matching(feats_dir=None)
        self.assertEqual(result.pairs_processed, 0)


if __name__ == "__main__":
    unittest.main()
