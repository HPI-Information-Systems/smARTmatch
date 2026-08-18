"""Immutable telemetry build-provenance artifact tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from telemetry import build_provenance, telemetry

_COMMIT = "1" * 40


class BuildProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app_root = self.root / "app"
        (self.app_root / "matching_pipeline").mkdir(parents=True)
        (self.app_root / "scripts").mkdir()
        (self.app_root / "shared").mkdir()
        (self.app_root / "telemetry").mkdir()
        (self.app_root / "requirements.txt").write_text("root==1\n")
        (self.app_root / "matching_pipeline/module.py").write_text("VALUE = 1\n")
        (self.app_root / "scripts/run_pipeline_scheduler.py").write_text(
            "print('scheduler')\n"
        )
        (self.app_root / "shared/helper.py").write_text("HELPER = True\n")
        (self.app_root / "telemetry/worker.py").write_text("WORKER = True\n")
        (self.app_root / "telemetry/telemetry-build-provenance.json").write_text(
            "tracked source input\n"
        )

    def test_artifact_captures_commit_and_exact_copied_source(self) -> None:
        first = build_provenance.build_provenance(self.app_root, _COMMIT)
        second = build_provenance.build_provenance(self.app_root, _COMMIT)

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(
            first["git"],
            {
                "commit": _COMMIT,
                "source": "docker_build_context_git_refs",
            },
        )
        self.assertRegex(first["source"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(first["source"]["file_count"], 6)

        (self.app_root / "telemetry/worker.py").write_text("WORKER = False\n")
        changed = build_provenance.build_provenance(self.app_root, _COMMIT)
        self.assertNotEqual(changed["source"]["sha256"], first["source"]["sha256"])
        self.assertEqual(changed["git"]["commit"], _COMMIT)

    def test_commit_is_required_and_strictly_validated(self) -> None:
        for value in ("", "unknown", "g" * 40, "1" * 39, "1" * 41):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "Git commit"
            ):
                build_provenance.build_provenance(self.app_root, value)
        self.assertEqual(
            build_provenance.build_provenance(self.app_root, "A" * 40)["git"]["commit"],
            "a" * 40,
        )

    def test_resolve_git_head_reads_loose_symbolic_reference(self) -> None:
        git_dir = self.root / "loose.git"
        (git_dir / "refs/heads").mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        (git_dir / "refs/heads/main").write_text(f"{_COMMIT}\n")

        self.assertEqual(build_provenance.resolve_git_head(git_dir), _COMMIT)

    def test_resolve_git_head_reads_packed_and_detached_references(self) -> None:
        git_dir = self.root / "packed.git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        (git_dir / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{_COMMIT} refs/heads/main\n"
            f"{'2' * 40} refs/tags/release\n"
            f"^{'4' * 40}\n"
        )
        self.assertEqual(build_provenance.resolve_git_head(git_dir), _COMMIT)

        detached = "a" * 64
        (git_dir / "HEAD").write_text(f"{detached}\n")
        self.assertEqual(build_provenance.resolve_git_head(git_dir), detached)

    def test_resolve_git_head_rejects_unsafe_or_external_metadata(self) -> None:
        git_dir = self.root / "unsafe.git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/../../outside\n")
        with self.assertRaisesRegex(ValueError, "invalid ref"):
            build_provenance.resolve_git_head(git_dir)

        pointer = self.root / "worktree.git"
        pointer.write_text("gitdir: /outside/build-context\n")
        with self.assertRaisesRegex(ValueError, "standalone checkout"):
            build_provenance.resolve_git_head(pointer)

        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        outside = self.root / "outside-packed-refs"
        outside.write_text(f"{_COMMIT} refs/heads/main\n")
        (git_dir / "packed-refs").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "packed-refs escapes"):
            build_provenance.resolve_git_head(git_dir)

    def test_resolve_git_head_rejects_symbolic_reference_cycle(self) -> None:
        git_dir = self.root / "cycle.git"
        (git_dir / "refs/heads").mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        (git_dir / "refs/heads/main").write_text("ref: refs/heads/other\n")
        (git_dir / "refs/heads/other").write_text("ref: refs/heads/main\n")

        with self.assertRaisesRegex(ValueError, "cycle"):
            build_provenance.resolve_git_head(git_dir)

    def test_runtime_identity_revalidates_baked_source(self) -> None:
        artifact = self.root / "telemetry-build-provenance.json"
        runtime_app_root = Path(telemetry.__file__).resolve().parents[1]
        build_provenance.write_provenance(
            artifact,
            build_provenance.build_provenance(runtime_app_root, _COMMIT),
        )
        with mock.patch.dict(
            os.environ,
            {
                "SMARTMATCH_BUILD_PROVENANCE_FILE": str(artifact),
                "SMARTMATCH_REQUIRE_BUILD_PROVENANCE": "true",
            },
            clear=True,
        ), mock.patch.object(telemetry.subprocess, "run") as run_git:
            identity = telemetry._git_identity()

        self.assertEqual(identity["commit"], _COMMIT)
        self.assertEqual(identity["source"], "build_provenance")
        self.assertRegex(identity["build_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(identity["worktree_state"], "captured_by_build_source_sha256")
        run_git.assert_not_called()

    def test_runtime_detects_source_changed_after_artifact_generation(self) -> None:
        artifact = self.root / "telemetry-build-provenance.json"
        document = build_provenance.build_provenance(self.app_root, _COMMIT)
        build_provenance.write_provenance(artifact, document)
        document["source"]["sha256"] = "f" * 64
        build_provenance.write_provenance(artifact, document)
        with mock.patch.dict(
            os.environ,
            {"SMARTMATCH_BUILD_PROVENANCE_FILE": str(artifact)},
            clear=True,
        ), mock.patch.object(
            telemetry,
            "source_snapshot",
            return_value=build_provenance.source_snapshot(self.app_root),
        ), self.assertRaisesRegex(
            ValueError, "does not match"
        ):
            telemetry._build_provenance_identity(artifact)

    def test_runtime_rejects_non_string_commit(self) -> None:
        artifact = self.root / "numeric-commit.json"
        document = build_provenance.build_provenance(self.app_root, _COMMIT)
        document["git"]["commit"] = int(_COMMIT)
        build_provenance.write_provenance(artifact, document)
        with mock.patch.object(
            telemetry,
            "source_snapshot",
            return_value=build_provenance.source_snapshot(self.app_root),
        ), self.assertRaisesRegex(ValueError, "non-canonical"):
            telemetry._build_provenance_identity(artifact)

    def test_configured_artifact_fails_closed_when_malformed(self) -> None:
        artifact = self.root / "bad.json"
        artifact.write_text(json.dumps({"schema_version": 1, "git": {}}))
        with mock.patch.dict(
            os.environ,
            {"SMARTMATCH_BUILD_PROVENANCE_FILE": str(artifact)},
            clear=True,
        ), self.assertRaisesRegex(ValueError, "unsupported schema"):
            telemetry._git_identity()

    def test_required_artifact_cannot_be_disabled_by_blank_path(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "SMARTMATCH_BUILD_PROVENANCE_FILE": " ",
                "SMARTMATCH_REQUIRE_BUILD_PROVENANCE": "true",
            },
            clear=True,
        ), self.assertRaisesRegex(ValueError, "requires baked"):
            telemetry._git_identity()


if __name__ == "__main__":
    unittest.main()
