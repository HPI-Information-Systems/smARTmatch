"""Frontend container entrypoint."""

from shared.logging_adapter import configure_logging


def main() -> None:
    configure_logging()
    from .app import main as run_frontend

    run_frontend()


if __name__ == "__main__":
    raise SystemExit(main())
