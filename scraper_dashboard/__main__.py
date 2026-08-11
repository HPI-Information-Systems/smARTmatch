import logging
import os

from waitress import serve

from .app import app

HOST = "0.0.0.0"
PORT = 5555


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    print(
        "Scraper dashboard DB: "
        f"{os.getenv('POSTGRES_USER')}@{os.getenv('POSTGRES_HOST')}:"
        f"{os.getenv('POSTGRES_PORT')}/{os.getenv('POSTGRES_DB')}",
        flush=True,
    )

    print(f"Scraper dashboard listening on http://{HOST}:{PORT}", flush=True)
    serve(app, host=HOST, port=PORT)
