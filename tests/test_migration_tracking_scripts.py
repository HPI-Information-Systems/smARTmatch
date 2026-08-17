from __future__ import annotations

import os
from pathlib import Path
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[1]
APPLY_SCRIPT = ROOT / "scripts" / "apply_production_migration.sh"
LATEST_SCRIPT = ROOT / "scripts" / "latest_applied_migration.sh"


def _fake_environment(tmp_path: Path, *, latest: str = "") -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    stdin_log = tmp_path / "stdin.log"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"

            if [[ " $* " == *" pg_dump "* ]]; then
              if [[ "${FAKE_PG_DUMP_FAIL:-0}" == "1" ]]; then
                exit 1
              fi
              printf 'fake-dump'
              exit 0
            fi

            sql_stdin=''
            if [[ " $* " == *" psql "* && " $* " != *" -c "* ]]; then
              sql_stdin="$(cat)"
              printf '%s\\n' "$sql_stdin" >> "$FAKE_DOCKER_LOG"
            fi
            combined_sql="$* $sql_stdin"

            # Match real psql behavior: -v placeholders are not expanded in -c SQL.
            if [[ " $* " == *" -c "* && "$*" == *":'"* ]]; then
              printf 'syntax error at or near ":"\\n' >&2
              exit 1
            fi
            if [[ "$combined_sql" == *"to_regclass('public.schema_migrations')"* ]]; then
              printf '%s\\n' "${FAKE_LEDGER_EXISTS:-t}"
              exit 0
            fi
            if [[ "$combined_sql" == *"SELECT migration_name"* && "$combined_sql" == *"WHERE status = 'applied'"* && "$combined_sql" == *"ORDER BY application_order"* ]]; then
              printf '%s\\n' "${FAKE_LATEST_MIGRATION:-}"
              exit 0
            fi
            if [[ "$combined_sql" == *"SELECT status || '|' || checksum_sha256"* ]]; then
              printf '%s' "${FAKE_EXISTING_MIGRATION:-}"
              exit 0
            fi
            if [[ "$combined_sql" == *"WHERE status <> 'applied'"* ]]; then
              printf '%s' "${FAKE_INCOMPLETE_MIGRATIONS:-}"
              exit 0
            fi
            if [[ "$combined_sql" == *"RETURNING migration_name"* ]]; then
              previous=''
              for argument in "$@"; do
                if [[ "$previous" == "-v" && "$argument" == migration_name=* ]]; then
                  printf '%s\\n' "${argument#migration_name=}"
                  exit 0
                fi
                previous="$argument"
              done
              exit 1
            fi
            if [[ -n "$sql_stdin" && " $* " != *" migration_name="* ]]; then
              printf '%s\\n' "$sql_stdin" >> "$FAKE_STDIN_LOG"
            fi
            """
        ),
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env_file = tmp_path / "database.env"
    env_file.write_text(
        "POSTGRES_USER=smartmatch\n"
        "POSTGRES_DB=smartmatch_production\n"
        "POSTGRES_PASSWORD=test-secret\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "ENV_FILE": str(env_file),
            "BACKUP_DIR": str(tmp_path / "backups"),
            "MAINTENANCE_LOCK_DIR": str(tmp_path / ".data-maintenance.lock"),
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_STDIN_LOG": str(stdin_log),
            "FAKE_LEDGER_EXISTS": "t",
            "FAKE_LATEST_MIGRATION": latest,
        }
    )
    return env


def test_migration_scripts_have_valid_bash_syntax() -> None:
    for script in (APPLY_SCRIPT, LATEST_SCRIPT):
        subprocess.run(["bash", "-n", str(script)], check=True)

    # macOS still ships Bash 3.2, which does not support associative arrays.
    assert "declare -A" not in APPLY_SCRIPT.read_text(encoding="utf-8")


def test_latest_script_prints_only_latest_applied_filename(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path, latest="21_mark_cleaned_up_image_files.sql")

    result = subprocess.run(
        [str(LATEST_SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout == "21_mark_cleaned_up_image_files.sql\n"
    assert result.stderr == ""


def test_latest_script_fails_when_ledger_is_missing(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path)
    env["FAKE_LEDGER_EXISTS"] = "f"

    result = subprocess.run(
        [str(LATEST_SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "No migration ledger found" in result.stderr


def test_apply_script_requires_an_explicit_migration(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path)

    result = subprocess.run(
        [str(APPLY_SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert "no default migration is selected" in result.stderr
    assert "Usage:" in result.stderr
    assert not Path(env["FAKE_DOCKER_LOG"]).exists()
    assert not Path(env["MAINTENANCE_LOCK_DIR"]).exists()


def test_apply_script_records_a_successful_migration(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path)
    migration = tmp_path / "99_test_migration.sql"
    migration_sql = "BEGIN;\nSELECT 1;\nCOMMIT;\n"
    migration.write_text(migration_sql, encoding="utf-8")

    result = subprocess.run(
        [str(APPLY_SCRIPT), str(migration)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    docker_log = Path(env["FAKE_DOCKER_LOG"]).read_text(encoding="utf-8")
    stdin_log = Path(env["FAKE_STDIN_LOG"]).read_text(encoding="utf-8")
    assert stdin_log == migration_sql
    assert "CREATE TABLE IF NOT EXISTS public.schema_migrations" in docker_log
    assert "status = 'applied'" in docker_log
    assert "Applying migration:" in result.stdout
    assert not Path(env["MAINTENANCE_LOCK_DIR"]).exists()


def test_apply_script_blocks_migration_while_another_row_is_incomplete(
    tmp_path: Path,
) -> None:
    env = _fake_environment(tmp_path)
    env["FAKE_INCOMPLETE_MIGRATIONS"] = (
        "20_prepare_image_cleanup.sql|failed\n19_previous_attempt.sql|applying"
    )
    migration = tmp_path / "21_mark_cleaned_up_image_files.sql"
    migration.write_text("SELECT 1;\n", encoding="utf-8")

    result = subprocess.run(
        [str(APPLY_SCRIPT), str(migration)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "while other migration ledger rows are incomplete" in result.stderr
    assert "20_prepare_image_cleanup.sql (failed)" in result.stderr
    assert "19_previous_attempt.sql (applying)" in result.stderr
    assert "use --force" in result.stderr
    assert "Applying migration:" not in result.stdout
    assert not Path(env["FAKE_STDIN_LOG"]).exists()


def test_apply_script_force_allows_migration_with_an_incomplete_row(
    tmp_path: Path,
) -> None:
    env = _fake_environment(tmp_path)
    env["FAKE_INCOMPLETE_MIGRATIONS"] = "20_prepare_image_cleanup.sql|failed"
    migration = tmp_path / "21_mark_cleaned_up_image_files.sql"
    migration_sql = "SELECT 1;\n"
    migration.write_text(migration_sql, encoding="utf-8")

    result = subprocess.run(
        [str(APPLY_SCRIPT), "--force", str(migration)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "--force is bypassing incomplete migration ledger rows" in result.stderr
    assert "20_prepare_image_cleanup.sql (failed)" in result.stderr
    assert "Applying migration:" in result.stdout
    assert Path(env["FAKE_STDIN_LOG"]).read_text(encoding="utf-8") == migration_sql


def test_apply_script_refuses_an_existing_maintenance_lock(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path)
    migration = tmp_path / "99_test_migration.sql"
    migration.write_text("SELECT 1;\n", encoding="utf-8")
    lock_dir = Path(env["MAINTENANCE_LOCK_DIR"])
    lock_dir.mkdir()

    result = subprocess.run(
        [str(APPLY_SCRIPT), str(migration)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "Another backup, restore, or migration may be running" in result.stderr
    assert lock_dir.is_dir()
    assert not Path(env["FAKE_DOCKER_LOG"]).exists()


def test_apply_script_releases_lock_after_backup_failure(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path)
    env["FAKE_PG_DUMP_FAIL"] = "1"
    migration = tmp_path / "99_test_migration.sql"
    migration.write_text("SELECT 1;\n", encoding="utf-8")

    result = subprocess.run(
        [str(APPLY_SCRIPT), str(migration)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "Backup failed" in result.stderr
    assert not Path(env["MAINTENANCE_LOCK_DIR"]).exists()


def test_baseline_records_without_executing_migration(tmp_path: Path) -> None:
    env = _fake_environment(tmp_path)
    migration = tmp_path / "98_existing_migration.sql"
    migration.write_text("SELECT 'must not execute';\n", encoding="utf-8")

    result = subprocess.run(
        [str(APPLY_SCRIPT), "--baseline", str(migration)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    stdin_log = Path(env["FAKE_STDIN_LOG"])
    assert not stdin_log.exists() or stdin_log.read_text(encoding="utf-8") == ""
    assert "Recording verified migration without executing it" in result.stdout
