"""Contracts for safe restore behavior and operator guidance."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import textwrap


_ROOT = Path(__file__).resolve().parents[1]
_RESTORE_SCRIPT = _ROOT / "scripts" / "restore.sh"


def test_printed_compose_commands_are_anchored_to_the_repository() -> None:
    source = _RESTORE_SCRIPT.read_text(encoding="utf-8")

    assert "docker compose --project-directory %q" in source
    assert '"$ROOT_DIR"' in source
    assert 'print_docker_compose_command stop "${application_services[@]}"' in source
    assert 'print_docker_compose_command up -d "${application_services[@]}"' in source


def _restore_environment(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path, dict[str, str]]:
    repository = tmp_path / "repository"
    scripts_dir = repository / "scripts"
    images_dir = repository / "db" / "images"
    backups_dir = repository / "backups"
    bin_dir = tmp_path / "bin"
    scripts_dir.mkdir(parents=True)
    images_dir.mkdir(parents=True)
    backups_dir.mkdir()
    bin_dir.mkdir()
    script = scripts_dir / "restore.sh"
    shutil.copy2(_RESTORE_SCRIPT, script)

    docker_log = tmp_path / "docker.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"

            if [[ "$*" == "compose ps --status running --quiet "* ]]; then
                exit 0
            fi
            if [[ "$*" == "compose exec -T db pg_restore --file=/dev/null" ]]; then
                cat >/dev/null
                exit "${FAKE_VALIDATE_STATUS:-0}"
            fi
            if [[ "$*" == "compose exec -T db sh -c "* ]]; then
                cat >/dev/null
                exit "${FAKE_RESTORE_STATUS:-0}"
            fi

            printf 'Unexpected docker command: %s\n' "$*" >&2
            exit 1
            """
        ),
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "FAKE_DOCKER_LOG": str(docker_log),
        }
    )
    return script, repository, images_dir, backups_dir, docker_log, env


def _run_restore(
    script: Path,
    repository: Path,
    env: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *arguments],
        cwd=repository,
        env=env,
        text=True,
        capture_output=True,
    )


def test_only_db_dump_restores_database_without_replacing_images(tmp_path: Path) -> None:
    script, repository, images_dir, backups_dir, docker_log, env = (
        _restore_environment(tmp_path)
    )
    live_image = images_dir / "live.jpg"
    live_image.write_bytes(b"live-image")
    dump_path = backups_dir / "migration-backup.dmp"
    dump_path.write_bytes(b"PGDMPfake-dump")

    result = _run_restore(
        script, repository, env, "--only-db-dump", str(dump_path)
    )

    assert result.returncode == 0, result.stderr
    assert live_image.read_bytes() == b"live-image"
    assert "Database-only restore complete" in result.stdout
    assert f"Restored database from: {dump_path}" in result.stdout
    assert f"Live images were not changed: {images_dir}" in result.stdout
    assert not (backups_dir / ".data-maintenance.lock").exists()

    docker_commands = docker_log.read_text(encoding="utf-8")
    assert "compose exec -T db pg_restore --file=/dev/null" in docker_commands
    assert "dropdb --force --if-exists" in docker_commands
    assert "pg_restore --exit-on-error --single-transaction" in docker_commands


def test_invalid_dump_is_rejected_before_database_replacement(tmp_path: Path) -> None:
    script, repository, images_dir, backups_dir, docker_log, env = (
        _restore_environment(tmp_path)
    )
    live_image = images_dir / "live.jpg"
    live_image.write_bytes(b"live-image")
    dump_path = backups_dir / "invalid.dmp"
    dump_path.write_bytes(b"not-a-postgres-dump")

    result = _run_restore(
        script, repository, env, "--only-db-dump", str(dump_path)
    )

    assert result.returncode == 1
    assert "invalid or unreadable custom-format PostgreSQL dump" in result.stderr
    assert live_image.read_bytes() == b"live-image"
    assert "dropdb --force --if-exists" not in docker_log.read_text(encoding="utf-8")
    assert not (backups_dir / ".data-maintenance.lock").exists()
    assert not list(backups_dir.glob(".db-dump-restore.*"))


def test_database_restore_failure_leaves_images_and_releases_lock(
    tmp_path: Path,
) -> None:
    script, repository, images_dir, backups_dir, _docker_log, env = (
        _restore_environment(tmp_path)
    )
    live_image = images_dir / "live.jpg"
    live_image.write_bytes(b"live-image")
    dump_path = backups_dir / "migration-backup.dmp"
    dump_path.write_bytes(b"PGDMPfake-dump")
    env["FAKE_RESTORE_STATUS"] = "1"

    result = _run_restore(
        script, repository, env, "--only-db-dump", str(dump_path)
    )

    assert result.returncode == 1
    assert "database replacement failed; live images were not changed" in result.stderr
    assert live_image.read_bytes() == b"live-image"
    assert not (backups_dir / ".data-maintenance.lock").exists()
    assert not list(backups_dir.glob(".db-dump-restore.*"))


def test_default_mode_still_replaces_database_and_images(tmp_path: Path) -> None:
    script, repository, images_dir, backups_dir, _docker_log, env = (
        _restore_environment(tmp_path)
    )
    (images_dir / "old.jpg").write_bytes(b"old-image")
    backup_path = backups_dir / "complete"
    backup_images = backup_path / "db" / "images"
    backup_images.mkdir(parents=True)
    (backup_path / "db_dump.dump").write_bytes(b"PGDMPfake-dump")
    (backup_images / "restored.jpg").write_bytes(b"restored-image")

    result = _run_restore(script, repository, env, str(backup_path))

    assert result.returncode == 0, result.stderr
    assert not (images_dir / "old.jpg").exists()
    assert (images_dir / "restored.jpg").read_bytes() == b"restored-image"
    assert "Restore complete" in result.stdout
    assert f"Restored database and images from: {backup_path}" in result.stdout
    assert not (backups_dir / ".data-maintenance.lock").exists()
