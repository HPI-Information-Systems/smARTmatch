"""Static contracts for safe restore-script operator guidance."""

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_RESTORE_SCRIPT = _ROOT / "scripts" / "restore.sh"


def test_printed_compose_commands_are_anchored_to_the_repository() -> None:
    source = _RESTORE_SCRIPT.read_text(encoding="utf-8")

    assert "docker compose --project-directory %q" in source
    assert '"$ROOT_DIR"' in source
    assert 'print_docker_compose_command stop "${application_services[@]}"' in source
    assert 'print_docker_compose_command up -d "${application_services[@]}"' in source
