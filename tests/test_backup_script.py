from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = ROOT / "scripts" / "backup.sh"


def _backup_environment(
    tmp_path: Path, *, running_services: str = "scrapers,matching_pipeline"
) -> tuple[Path, Path, dict[str, str]]:
    repository = tmp_path / "repository"
    scripts_dir = repository / "scripts"
    images_dir = repository / "db" / "images"
    bin_dir = tmp_path / "bin"
    scripts_dir.mkdir(parents=True)
    images_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(BACKUP_SCRIPT, scripts_dir / "backup.sh")
    (images_dir / "image.jpg").write_bytes(b"test-image")

    docker_log = tmp_path / "docker.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"

            if [[ "$*" == "compose ps --status running --quiet "* ]]; then
                service="${*: -1}"
                case ",${FAKE_RUNNING_SERVICES:-}," in
                    *",$service,"*) printf '%s-container\n' "$service" ;;
                esac
                exit 0
            fi
            if [[ "$*" == "compose stop "* ]]; then
                exit "${FAKE_STOP_STATUS:-0}"
            fi
            if [[ "$*" == "compose start "* ]]; then
                exit "${FAKE_START_STATUS:-0}"
            fi
            if [[ "$*" == *"compose exec"* && "$*" == *"pg_dump"* ]]; then
                if [[ "${FAKE_PG_DUMP_FAIL:-0}" == "1" ]]; then
                    exit 1
                fi
                printf 'PGDMPfake-dump'
                exit 0
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
            "FAKE_RUNNING_SERVICES": running_services,
        }
    )
    return scripts_dir / "backup.sh", repository, env


def _run_backup(
    tmp_path: Path,
    *,
    running_services: str = "scrapers,matching_pipeline",
    dump_fails: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, dict[str, str]]:
    script, repository, env = _backup_environment(
        tmp_path, running_services=running_services
    )
    if dump_fails:
        env["FAKE_PG_DUMP_FAIL"] = "1"
    backup_path = tmp_path / "backup"
    result = subprocess.run(
        [str(script), str(backup_path)],
        cwd=repository,
        env=env,
        text=True,
        capture_output=True,
    )
    return result, backup_path, repository, env


def test_backup_stops_and_restarts_all_running_image_writers(tmp_path: Path) -> None:
    result, backup_path, repository, env = _run_backup(tmp_path)

    assert result.returncode == 0, result.stderr
    docker_commands = Path(env["FAKE_DOCKER_LOG"]).read_text(encoding="utf-8")
    assert "compose stop scrapers matching_pipeline" in docker_commands
    assert "compose start scrapers" in docker_commands
    assert "compose start matching_pipeline" in docker_commands
    assert "Service confirmed stopped for backup: scrapers" in result.stdout
    assert "Service confirmed stopped for backup: matching_pipeline" in result.stdout
    assert "Service restarted after backup: scrapers" in result.stdout
    assert "Service restarted after backup: matching_pipeline" in result.stdout
    assert result.stdout.index("[2/5] Creating PostgreSQL dump") < result.stdout.index(
        "[3/5] Copying images"
    )
    assert result.stdout.index("[3/5] Copying images") < result.stdout.index(
        "[5/5] Restarting services"
    )
    assert (backup_path / "db_dump.dump").is_file()
    assert (backup_path / "db" / "images" / "image.jpg").is_file()
    assert not (repository / "backups" / ".data-maintenance.lock").exists()


def test_backup_restarts_only_services_that_were_running(tmp_path: Path) -> None:
    result, _, _, env = _run_backup(tmp_path, running_services="matching_pipeline")

    assert result.returncode == 0, result.stderr
    docker_commands = Path(env["FAKE_DOCKER_LOG"]).read_text(encoding="utf-8")
    assert "compose stop scrapers matching_pipeline" in docker_commands
    assert "compose start matching_pipeline" in docker_commands
    assert "compose start scrapers" not in docker_commands
    assert (
        "Service was already stopped and will remain stopped: scrapers" in result.stdout
    )


def test_backup_failure_restarts_services_and_removes_partial_backup(
    tmp_path: Path,
) -> None:
    result, backup_path, repository, env = _run_backup(tmp_path, dump_fails=True)

    assert result.returncode == 1
    assert "database dump failed" in result.stderr
    docker_commands = Path(env["FAKE_DOCKER_LOG"]).read_text(encoding="utf-8")
    assert "compose start scrapers" in docker_commands
    assert "compose start matching_pipeline" in docker_commands
    assert (
        "Backup exiting; restarting services that were running before backup"
        in result.stdout
    )
    assert "Service restarted after backup: scrapers" in result.stdout
    assert "Service restarted after backup: matching_pipeline" in result.stdout
    assert not backup_path.exists()
    assert not (repository / "backups" / ".data-maintenance.lock").exists()
