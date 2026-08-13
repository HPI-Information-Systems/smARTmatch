"""Scraper dashboard container entrypoint."""

import os

from shared.logging_adapter import configure_logging, get_logger

HOST = "0.0.0.0"
PORT = 5555
logger = get_logger(__name__)


def main() -> int:
    configure_logging()

    from waitress import serve

    from .app import app

    logger.info(
        "Scraper dashboard DB: %s@%s:%s/%s",
        os.getenv("POSTGRES_USER"),
        os.getenv("POSTGRES_HOST"),
        os.getenv("POSTGRES_PORT"),
        os.getenv("POSTGRES_DB"),
    )
    logger.info("Scraper dashboard listening on http://%s:%d", HOST, PORT)
    serve(app, host=HOST, port=PORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
