#!/usr/bin/env python3
"""Backfill missing auction image_file links while preserving existing filenames."""

from __future__ import annotations

import argparse
import csv
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_BACKUP_DIR = REPO_ROOT / "local" / "db-backups"
DEFAULT_MANIFEST_DIR = REPO_ROOT / "local"
EXTENSION_RE = re.compile(r"^[a-z0-9]{1,5}$")
NO_EXTENSION = "none"


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str


@dataclass(frozen=True)
class SourceImage:
    auction_artwork_id: str
    image_index: int
    source_img_path: str
    file_extension: str


@dataclass(frozen=True)
class BackfilledImage:
    source: SourceImage
    image_file_id: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create image_file and auction_artwork_image_file rows from "
            "auction_artwork.img_paths for artworks without existing image-file links. "
            "The original path is stored in image_file.file_path so files do not "
            "need to be renamed to their numeric image_file_id."
        )
    )
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-backup",
        action="store_true",
        help="Skip pg_dump backup before writing (not recommended).",
    )
    parser.add_argument(
        "--limit-artworks",
        type=int,
        help="Limit the number of auction artworks considered, useful for testing.",
    )
    parser.add_argument(
        "--source-root",
        default=str(REPO_ROOT),
        help="Base directory for relative auction_artwork.img_paths in the manifest.",
    )
    parser.add_argument(
        "--manifest",
        help=(
            "CSV manifest path. Defaults to "
            "local/auction_image_backfill_manifest_<timestamp>.csv."
        ),
    )
    parser.add_argument("--no-manifest", action="store_true")
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Environment file not found: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        try:
            parsed = shlex.split(value, comments=True, posix=True)
            value = parsed[0] if parsed else ""
        except ValueError:
            value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def db_config() -> DbConfig:
    required = [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    ]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return DbConfig(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ["POSTGRES_PORT"]),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def connect(config: DbConfig) -> psycopg.Connection:
    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.dbname,
        user=config.user,
        password=config.password,
        row_factory=dict_row,
    )


def create_backup(config: DbConfig) -> Path:
    DEFAULT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = DEFAULT_BACKUP_DIR / f"{config.dbname}_{timestamp}.dmp"
    cmd = [
        "pg_dump",
        "-h",
        config.host,
        "-p",
        str(config.port),
        "-U",
        config.user,
        "-d",
        config.dbname,
        "-Fc",
        "-f",
        str(output),
    ]
    env = os.environ.copy()
    env["PGPASSWORD"] = config.password
    print(f"Creating backup: {output}")
    subprocess.run(cmd, check=True, env=env)
    return output


def fetch_source_images(conn: psycopg.Connection, limit_artworks: int | None) -> list[SourceImage]:
    limit_sql = "LIMIT %s" if limit_artworks is not None else ""
    params: tuple[int, ...] = (limit_artworks,) if limit_artworks is not None else ()
    query = f"""
        WITH candidate_artworks AS (
            SELECT a.auction_artwork_id, a.img_paths
            FROM auction_artwork a
            WHERE COALESCE(array_length(a.img_paths, 1), 0) > 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM auction_artwork_image_file aaif
                  WHERE aaif.auction_artwork_id = a.auction_artwork_id
              )
            ORDER BY a.auction_artwork_id::text
            {limit_sql}
        )
        SELECT
            a.auction_artwork_id::text AS auction_artwork_id,
            p.ordinality::integer AS image_index,
            btrim(p.img_path) AS source_img_path
        FROM candidate_artworks a
        CROSS JOIN LATERAL unnest(a.img_paths) WITH ORDINALITY AS p(img_path, ordinality)
        WHERE btrim(COALESCE(p.img_path, '')) <> ''
        ORDER BY a.auction_artwork_id::text, p.ordinality
    """
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [_source_image(row) for row in rows]


def _source_image(row: dict[str, Any]) -> SourceImage:
    source_path = str(row["source_img_path"])
    return SourceImage(
        auction_artwork_id=str(row["auction_artwork_id"]),
        image_index=int(row["image_index"]),
        source_img_path=source_path,
        file_extension=file_extension_from_path(source_path),
    )


def file_extension_from_path(path: str) -> str:
    parsed_path = urlparse(path).path or path
    suffix = Path(parsed_path).suffix.lower().lstrip(".")
    if not suffix:
        return NO_EXTENSION
    if not EXTENSION_RE.fullmatch(suffix):
        raise ValueError(f"Invalid file extension {suffix!r} in image path: {path}")
    return suffix


def insert_backfill_rows(
    conn: psycopg.Connection, source_images: list[SourceImage]
) -> list[BackfilledImage]:
    if not source_images:
        return []
    with conn.transaction():
        with conn.cursor() as cur:
            ensure_file_path_column(cur)
            cur.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence('image_file', 'image_file_id'),
                    (SELECT GREATEST(COALESCE(MAX(image_file_id), 1), 1) FROM image_file),
                    true
                )
                """
            )
            cur.execute(
                """
                SELECT nextval(pg_get_serial_sequence('image_file', 'image_file_id'))::integer AS id
                FROM generate_series(1, %s)
                """,
                (len(source_images),),
            )
            ids = [int(row["id"]) for row in cur.fetchall()]
            backfilled = [
                BackfilledImage(source=source, image_file_id=image_id)
                for source, image_id in zip(source_images, ids, strict=True)
            ]
            cur.executemany(
                """
                INSERT INTO image_file (image_file_id, file_extension, file_path)
                VALUES (%s, %s, %s)
                """,
                [
                    (row.image_file_id, row.source.file_extension, row.source.source_img_path)
                    for row in backfilled
                ],
            )
            cur.executemany(
                """
                INSERT INTO auction_artwork_image_file (
                    auction_artwork_id,
                    image_file_id,
                    is_image_matching_processed
                )
                VALUES (%s, %s, false)
                """,
                [(row.source.auction_artwork_id, row.image_file_id) for row in backfilled],
            )
    return backfilled


def ensure_file_path_column(cur: psycopg.Cursor) -> None:
    cur.execute("ALTER TABLE image_file ADD COLUMN IF NOT EXISTS file_path text")


def preview_backfill_rows(
    conn: psycopg.Connection, source_images: list[SourceImage]
) -> list[BackfilledImage]:
    with conn.cursor() as cur:
        cur.execute("SELECT GREATEST(COALESCE(MAX(image_file_id), 1), 1) AS max_id FROM image_file")
        next_id = int(cur.fetchone()["max_id"]) + 1
    return [
        BackfilledImage(source=source, image_file_id=next_id + index)
        for index, source in enumerate(source_images)
    ]


def manifest_path(args: argparse.Namespace) -> Path:
    if args.manifest:
        return Path(args.manifest).expanduser()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_MANIFEST_DIR / f"auction_image_backfill_manifest_{timestamp}.csv"


def source_abs_path(source_root: Path, raw_path: str) -> str:
    parsed = urlparse(raw_path)
    if parsed.scheme and parsed.scheme != "file":
        return ""
    path = Path(parsed.path if parsed.scheme == "file" else raw_path).expanduser()
    if not path.is_absolute():
        path = source_root / path
    return str(path.resolve())


def write_manifest(
    path: Path,
    rows: list[BackfilledImage],
    *,
    source_root: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "auction_artwork_id",
        "image_index",
        "source_img_path",
        "resolved_source_path",
        "source_file_exists",
        "image_file_id",
        "file_extension",
        "image_file_file_path",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            source_path = source_abs_path(source_root, row.source.source_img_path)
            writer.writerow(
                {
                    "auction_artwork_id": row.source.auction_artwork_id,
                    "image_index": row.source.image_index,
                    "source_img_path": row.source.source_img_path,
                    "resolved_source_path": source_path,
                    "source_file_exists": bool(source_path and Path(source_path).is_file()),
                    "image_file_id": row.image_file_id,
                    "file_extension": row.source.file_extension,
                    "image_file_file_path": row.source.source_img_path,
                }
            )


def run(args: argparse.Namespace, config: DbConfig) -> int:
    source_root = Path(args.source_root).expanduser().resolve()
    with connect(config) as conn:
        source_images = fetch_source_images(conn, args.limit_artworks)
        if not source_images:
            print(
                "No auction_artwork.img_paths entries without "
                "auction_artwork_image_file links found."
            )
            return 0
        print(f"Auction image rows to backfill: {len(source_images)}")
        affected_artworks = {row.auction_artwork_id for row in source_images}
        print(f"Auction artworks affected: {len(affected_artworks)}")
        if args.dry_run:
            preview = preview_backfill_rows(conn, source_images[:10])
            print("Dry run: no database rows inserted and no manifest written.")
            print("A real run will add image_file.file_path if the column is missing.")
            print("First planned image_file rows:")
            for row in preview:
                print(
                    "  "
                    f"image_file_id={row.image_file_id}, "
                    f"file_path={row.source.source_img_path}"
                )
            return 0
        backfilled = insert_backfill_rows(conn, source_images)

    print(f"Created image_file rows: {len(backfilled)}")
    print(f"Created auction_artwork_image_file links: {len(backfilled)}")
    if backfilled and not args.no_manifest:
        output = manifest_path(args)
        write_manifest(output, backfilled, source_root=source_root)
        print(f"Wrote backfill manifest: {output}")
    return 0


def main() -> int:
    args = parse_args()
    load_env_file(Path(args.env_file))
    config = db_config()
    if not args.dry_run and not args.skip_backup:
        create_backup(config)
    return run(args, config)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
