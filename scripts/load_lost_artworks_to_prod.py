#!/usr/bin/env python3
"""Load non-production lost-artwork rows into the production schema.

The script reads repository-root `.env`, connects to production with POSTGRES_*
variables, connects to non-production with NON_PROD_POSTGRES_* variables, copies
referenced metadata rows, converts legacy lost_artwork image paths into
production `image_file` + `lost_artwork_image_file` rows, and writes all
production changes in one transaction.

Image files are not copied by default. A CSV manifest is written after a
successful commit so files can be copied into SMARTMATCH_IMAGES_DIR while preserving
source filenames. The import fails if flattening into SMARTMATCH_IMAGES_DIR would
cause conflicting filenames from different source paths.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_MANIFEST_DIR = REPO_ROOT / "local"
DEFAULT_BACKUP_DIR = REPO_ROOT / "local" / "db-backups"

EXTENSION_RE = re.compile(r"^[a-z0-9]{1,5}$")

COUNTRY_COLUMNS = ("country_id", "country_name", "country_name_en")
LOCATION_COLUMNS = (
    "location_id",
    "location_name",
    "location_name_en",
    "country",
    "country_en",
    "raw_data",
    "lat",
    "lon",
    "historical_info",
    "display_name",
    "country_ids",
)
LOCATION_VARIANT_COLUMNS = ("name_variant_id", "location_id", "name_variant")
INSTITUTION_COLUMNS = (
    "institution_id",
    "name",
    "address",
    "phone",
    "fax",
    "website",
    "email",
    "contact_ids",
    "raw_data",
)
CONTACT_COLUMNS = (
    "contact_id",
    "first_name",
    "last_name",
    "role",
    "phone",
    "email",
    "institution_id",
)
ARTIST_COLUMNS = (
    "artist_id",
    "complete_name",
    "date_of_birth",
    "date_of_death",
    "place_of_birth",
    "place_of_death",
    "raw_data",
    "gender",
    "period_of_activity",
    "preferred_name",
    "surname",
    "title_of_nobility",
    "profession",
    "biographical_info",
    "associated_country_ids",
    "activity_place_ids",
)
ARTIST_VARIANT_COLUMNS = ("name_variant_id", "artist_id", "name_variant")
LITERATURE_COLUMNS = (
    "literature_id",
    "title",
    "author",
    "publishing_date",
    "publishing_location",
    "raw_data",
    "publishing_location_id",
)
SOURCE_LOST_COLUMNS = (
    "lost_artwork_id",
    "title",
    "title_en",
    "artist_ids",
    "depicted_person_ids",
    "img_paths",
    "width",
    "height",
    "width_frame",
    "height_frame",
    "depth",
    "diameter",
    "dating",
    "dating_start",
    "dating_end",
    "description",
    "description_en",
    "inventory_number",
    "material",
    "dict_material_name",
    "dict_material_id",
    "material_en",
    "technique",
    "dict_technique_name",
    "dict_technique_id",
    "technique_en",
    "provenance",
    "provenance_en",
    "institution_id",
    "circumstances_of_loss",
    "lost_art_id",
    "lost_art_url",
    "keywords",
    "literature_source_id",
    "literature_source_page",
    "type_of_loss",
    "restituted",
    "raw_data",
    "location_history",
)
TARGET_LOST_COLUMNS = (
    "lost_artwork_id",
    "title",
    "title_en",
    "artist_ids",
    "depicted_person_ids",
    "img_paths",
    "width",
    "height",
    "width_frame",
    "height_frame",
    "depth",
    "diameter",
    "dating",
    "dating_start",
    "dating_end",
    "description",
    "description_en",
    "inventory_number",
    "material",
    "dict_material_id",
    "material_en",
    "technique",
    "dict_technique_id",
    "technique_en",
    "provenance",
    "provenance_en",
    "institution_id",
    "circumstances_of_loss",
    "lost_art_id",
    "lost_art_url",
    "keywords",
    "literature_source_id",
    "literature_source_page",
    "type_of_loss",
    "restituted",
    "raw_data",
    "location_history",
)


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str


class DryRunRollback(Exception):
    """Raised internally to roll back a dry run."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load non-production lost_artwork rows into production."
    )
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument(
        "--only-lostart",
        action="store_true",
        help="Import only rows with lost_art_id or lost_art_url set.",
    )
    parser.add_argument("--lost-art-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-backup",
        action="store_true",
        help="Skip the production pg_dump backup (not recommended for writes).",
    )
    parser.add_argument(
        "--no-match-by-lost-art-id",
        action="store_true",
        help="Do not reuse an existing production row with the same lost_art_id.",
    )
    parser.add_argument(
        "--image-root",
        default=None,
        help="Main image directory. Defaults to SMARTMATCH_IMAGES_DIR from the repository .env.",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="CSV copy manifest path. Defaults to local/lost_artwork_image_copy_manifest_<timestamp>.csv.",
    )
    parser.add_argument("--no-manifest", action="store_true")
    parser.add_argument(
        "--allow-same-db",
        action="store_true",
        help="Allow source and target connection settings to point to the same DB.",
    )
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
        os.environ.setdefault(key, value)


def db_config(prefix: str) -> DbConfig:
    names = {
        "host": f"{prefix}POSTGRES_HOST",
        "port": f"{prefix}POSTGRES_PORT",
        "dbname": f"{prefix}POSTGRES_DB",
        "user": f"{prefix}POSTGRES_USER",
        "password": f"{prefix}POSTGRES_PASSWORD",
    }
    missing = [env_name for env_name in names.values() if not os.getenv(env_name)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    return DbConfig(
        host=os.environ[names["host"]],
        port=int(os.environ[names["port"]]),
        dbname=os.environ[names["dbname"]],
        user=os.environ[names["user"]],
        password=os.environ[names["password"]],
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


def same_db(a: DbConfig, b: DbConfig) -> bool:
    return (
        a.host == b.host
        and a.port == b.port
        and a.dbname == b.dbname
        and a.user == b.user
    )


def create_backup(config: DbConfig, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = backup_dir / f"{config.dbname}_{timestamp}.dmp"
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
    print(f"Creating production backup: {output}")
    subprocess.run(cmd, check=True, env=env)
    return output


def table_columns(conn: psycopg.Connection, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table,),
        )
        return {row["column_name"] for row in cur.fetchall()}


def existing_table(conn: psycopg.Connection, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (f"public.{table}",))
        return bool(cur.fetchone()["exists"])


def select_columns(available: set[str], desired: Iterable[str]) -> tuple[str, ...]:
    return tuple(column for column in desired if column in available)


def fetch_lost_rows(
    conn: psycopg.Connection, *, only_lostart: bool, lost_art_ids: list[str], limit: int | None
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    available = table_columns(conn, "lost_artwork")
    columns = select_columns(available, SOURCE_LOST_COLUMNS)
    if "lost_artwork_id" not in columns:
        raise RuntimeError("Source lost_artwork table lacks lost_artwork_id")

    conditions: list[sql.Composable] = []
    params: list[Any] = []
    if only_lostart:
        conditions.append(sql.SQL("(lost_art_id IS NOT NULL OR lost_art_url IS NOT NULL)"))
    if lost_art_ids:
        conditions.append(sql.SQL("lost_art_id = ANY(%s)"))
        params.append(lost_art_ids)

    query = sql.SQL("SELECT {cols} FROM lost_artwork").format(
        cols=sql.SQL(", ").join(map(sql.Identifier, columns))
    )
    if conditions:
        query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions)
    query += sql.SQL(" ORDER BY lost_artwork_id")
    if limit is not None:
        query += sql.SQL(" LIMIT %s")
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall(), columns


def fetch_by_ids(
    conn: psycopg.Connection,
    table: str,
    id_column: str,
    ids: set[Any],
    desired_columns: Iterable[str],
) -> list[dict[str, Any]]:
    ids = {value for value in ids if value is not None}
    if not ids or not existing_table(conn, table):
        return []
    columns = select_columns(table_columns(conn, table), desired_columns)
    if id_column not in columns:
        return []
    query = sql.SQL("SELECT {cols} FROM {table} WHERE {id_col} = ANY(%s)").format(
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
        table=sql.Identifier(table),
        id_col=sql.Identifier(id_column),
    )
    with conn.cursor() as cur:
        cur.execute(query, (list(ids),))
        return cur.fetchall()


def fetch_by_fk(
    conn: psycopg.Connection,
    table: str,
    fk_column: str,
    ids: set[Any],
    desired_columns: Iterable[str],
) -> list[dict[str, Any]]:
    ids = {value for value in ids if value is not None}
    if not ids or not existing_table(conn, table):
        return []
    columns = select_columns(table_columns(conn, table), desired_columns)
    if fk_column not in columns:
        return []
    query = sql.SQL("SELECT {cols} FROM {table} WHERE {fk_col} = ANY(%s)").format(
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
        table=sql.Identifier(table),
        fk_col=sql.Identifier(fk_column),
    )
    with conn.cursor() as cur:
        cur.execute(query, (list(ids),))
        return cur.fetchall()


def list_value(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [item for item in value if item is not None]
    return [value]


def add_ids(target: set[Any], value: Any) -> None:
    target.update(list_value(value))


def artist_name_key(value: Any) -> str:
    return str(value or "").strip().lower()


def build_artist_id_map(
    cur: psycopg.Cursor, artists: list[dict[str, Any]]
) -> tuple[dict[Any, Any], list[dict[str, Any]], int]:
    """Map source artist IDs onto production's case-insensitive name key."""

    by_name: dict[str, list[dict[str, Any]]] = {}
    unnamed: list[dict[str, Any]] = []
    for row in artists:
        key = artist_name_key(row.get("complete_name"))
        if key:
            by_name.setdefault(key, []).append(row)
        else:
            unnamed.append(row)

    existing_by_name: dict[str, Any] = {}
    if by_name:
        cur.execute(
            """
            SELECT lower(complete_name) AS name_key, artist_id
            FROM artist
            WHERE lower(complete_name) = ANY(%s)
            """,
            (list(by_name),),
        )
        existing_by_name = {row["name_key"]: row["artist_id"] for row in cur.fetchall()}

    artist_id_map: dict[Any, Any] = {}
    upsert_by_id: dict[Any, dict[str, Any]] = {}
    for key, rows in by_name.items():
        target_id = existing_by_name.get(key) or rows[0]["artist_id"]
        chosen = next((row for row in rows if row["artist_id"] == target_id), rows[0])
        upsert_row = dict(chosen)
        upsert_row["artist_id"] = target_id
        upsert_by_id[target_id] = upsert_row
        for row in rows:
            artist_id_map[row["artist_id"]] = target_id

    for row in unnamed:
        artist_id = row.get("artist_id")
        if artist_id is None:
            continue
        artist_id_map[artist_id] = artist_id
        upsert_by_id.setdefault(artist_id, row)

    remapped_count = sum(
        1 for source_id, target_id in artist_id_map.items() if source_id != target_id
    )
    return artist_id_map, list(upsert_by_id.values()), remapped_count


def remap_fk(value: Any, mapping: dict[Any, Any]) -> Any:
    return mapping.get(value, value)


def copy_supporting_rows(
    source: psycopg.Connection, target_cur: psycopg.Cursor, lost_rows: list[dict[str, Any]]
) -> tuple[dict[str, int], dict[Any, Any]]:
    artist_ids: set[Any] = set()
    institution_ids: set[Any] = set()
    literature_ids: set[Any] = set()
    for row in lost_rows:
        add_ids(artist_ids, row.get("artist_ids"))
        add_ids(artist_ids, row.get("depicted_person_ids"))
        if row.get("institution_id"):
            institution_ids.add(row["institution_id"])
        if row.get("literature_source_id"):
            literature_ids.add(row["literature_source_id"])

    institutions = fetch_by_ids(
        source, "institution", "institution_id", institution_ids, INSTITUTION_COLUMNS
    )
    literature = fetch_by_ids(
        source, "literature_source", "literature_id", literature_ids, LITERATURE_COLUMNS
    )
    artists = fetch_by_ids(source, "artist", "artist_id", artist_ids, ARTIST_COLUMNS)
    artist_id_map, artists_to_upsert, artist_remap_count = build_artist_id_map(
        target_cur, artists
    )

    location_ids: set[Any] = set()
    country_ids: set[Any] = set()
    contact_ids: set[Any] = set()
    for row in literature:
        if row.get("publishing_location_id"):
            location_ids.add(row["publishing_location_id"])
    for row in artists:
        if row.get("place_of_birth"):
            location_ids.add(row["place_of_birth"])
        if row.get("place_of_death"):
            location_ids.add(row["place_of_death"])
        add_ids(location_ids, row.get("activity_place_ids"))
        add_ids(country_ids, row.get("associated_country_ids"))
    for row in institutions:
        add_ids(contact_ids, row.get("contact_ids"))

    locations = fetch_by_ids(source, "location", "location_id", location_ids, LOCATION_COLUMNS)
    for row in locations:
        add_ids(country_ids, row.get("country_ids"))
    countries = fetch_by_ids(source, "country", "country_id", country_ids, COUNTRY_COLUMNS)

    contacts = fetch_by_fk(source, "contact", "institution_id", institution_ids, CONTACT_COLUMNS)
    contacts_by_id = {row["contact_id"]: row for row in contacts if row.get("contact_id")}
    for row in fetch_by_ids(source, "contact", "contact_id", contact_ids, CONTACT_COLUMNS):
        contacts_by_id[row["contact_id"]] = row
    contacts = list(contacts_by_id.values())

    location_variants = fetch_by_fk(
        source,
        "location_name_variants",
        "location_id",
        {row["location_id"] for row in locations if row.get("location_id")},
        LOCATION_VARIANT_COLUMNS,
    )
    source_artist_ids = {row["artist_id"] for row in artists if row.get("artist_id")}
    artist_variants = [
        dict(row)
        for row in fetch_by_fk(
            source,
            "artist_name_variants",
            "artist_id",
            source_artist_ids,
            ARTIST_VARIANT_COLUMNS,
        )
    ]
    for row in artist_variants:
        row["artist_id"] = remap_fk(row.get("artist_id"), artist_id_map)

    upsert_rows(target_cur, "country", ("country_id",), COUNTRY_COLUMNS, countries)
    upsert_rows(target_cur, "location", ("location_id",), LOCATION_COLUMNS, locations)
    upsert_rows(
        target_cur,
        "location_name_variants",
        ("name_variant_id",),
        LOCATION_VARIANT_COLUMNS,
        location_variants,
    )
    upsert_rows(
        target_cur, "institution", ("institution_id",), INSTITUTION_COLUMNS, institutions
    )
    upsert_rows(target_cur, "contact", ("contact_id",), CONTACT_COLUMNS, contacts)
    upsert_rows(
        target_cur, "literature_source", ("literature_id",), LITERATURE_COLUMNS, literature
    )
    upsert_rows(target_cur, "artist", ("artist_id",), ARTIST_COLUMNS, artists_to_upsert)
    upsert_rows(
        target_cur,
        "artist_name_variants",
        ("name_variant_id",),
        ARTIST_VARIANT_COLUMNS,
        artist_variants,
    )

    return {
        "countries": len(countries),
        "locations": len(locations),
        "location_variants": len(location_variants),
        "institutions": len(institutions),
        "contacts": len(contacts),
        "literature_sources": len(literature),
        "artists": len(artists_to_upsert),
        "artist_variants": len(artist_variants),
        "artist_ids_remapped_by_name": artist_remap_count,
    }, artist_id_map


def upsert_rows(
    cur: psycopg.Cursor,
    table: str,
    pk_columns: tuple[str, ...],
    desired_columns: Iterable[str],
    rows: list[dict[str, Any]],
) -> None:
    if not rows:
        return
    target_columns = table_columns(cur.connection, table)
    columns = select_columns(target_columns, desired_columns)
    if not all(pk in columns for pk in pk_columns):
        raise RuntimeError(f"Target table {table} lacks expected primary key columns")
    assignments = [col for col in columns if col not in pk_columns]
    if assignments:
        conflict_sql = sql.SQL("DO UPDATE SET ") + sql.SQL(", ").join(
            sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(col))
            for col in assignments
        )
    else:
        conflict_sql = sql.SQL("DO NOTHING")
    query = sql.SQL(
        "INSERT INTO {table} ({cols}) VALUES ({values}) ON CONFLICT ({pk}) {conflict}"
    ).format(
        table=sql.Identifier(table),
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
        values=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        pk=sql.SQL(", ").join(map(sql.Identifier, pk_columns)),
        conflict=conflict_sql,
    )
    cur.executemany(query, ([row.get(col) for col in columns] for row in rows))


def source_dict_rows(
    source: psycopg.Connection, table: str, id_col: str, ids: set[Any]
) -> list[dict[str, Any]]:
    name_col = "material_name" if table == "dict_material" else "technique_name"
    available = table_columns(source, table) if existing_table(source, table) else set()
    if {id_col, name_col}.issubset(available):
        return fetch_by_ids(source, table, id_col, ids, (id_col, name_col))
    return []


def ensure_dict_names(
    cur: psycopg.Cursor, table: str, id_col: str, name_col: str, names: set[str]
) -> dict[str, UUID]:
    names = {name for name in names if name}
    if not names:
        return {}
    cur.execute(
        sql.SQL("SELECT {id_col}, {name_col} FROM {table} WHERE {name_col} = ANY(%s)").format(
            id_col=sql.Identifier(id_col),
            name_col=sql.Identifier(name_col),
            table=sql.Identifier(table),
        ),
        (list(names),),
    )
    mapping = {row[name_col]: row[id_col] for row in cur.fetchall()}
    missing = sorted(names - set(mapping))
    if missing:
        query = sql.SQL("INSERT INTO {table} ({id_col}, {name_col}) VALUES (%s, %s)").format(
            table=sql.Identifier(table),
            id_col=sql.Identifier(id_col),
            name_col=sql.Identifier(name_col),
        )
        for name in missing:
            new_id = uuid4()
            cur.execute(query, (new_id, name))
            mapping[name] = new_id
    return mapping


def prepare_dictionaries(
    source: psycopg.Connection, target_cur: psycopg.Cursor, lost_rows: list[dict[str, Any]]
) -> tuple[dict[str, UUID], dict[str, UUID]]:
    material_ids: set[Any] = set()
    technique_ids: set[Any] = set()
    material_names: set[str] = set()
    technique_names: set[str] = set()
    for row in lost_rows:
        add_ids(material_ids, row.get("dict_material_id"))
        add_ids(technique_ids, row.get("dict_technique_id"))
        material_names.update(str(v) for v in list_value(row.get("dict_material_name")) if v)
        technique_names.update(str(v) for v in list_value(row.get("dict_technique_name")) if v)

    material_dicts = source_dict_rows(source, "dict_material", "dict_material_id", material_ids)
    technique_dicts = source_dict_rows(
        source, "dict_technique", "dict_technique_id", technique_ids
    )
    upsert_rows(
        target_cur,
        "dict_material",
        ("dict_material_id",),
        ("dict_material_id", "material_name"),
        material_dicts,
    )
    upsert_rows(
        target_cur,
        "dict_technique",
        ("dict_technique_id",),
        ("dict_technique_id", "technique_name"),
        technique_dicts,
    )

    material_name_map = ensure_dict_names(
        target_cur, "dict_material", "dict_material_id", "material_name", material_names
    )
    technique_name_map = ensure_dict_names(
        target_cur,
        "dict_technique",
        "dict_technique_id",
        "technique_name",
        technique_names,
    )
    return material_name_map, technique_name_map


def image_root_from_args(args: argparse.Namespace) -> Path:
    raw = args.image_root or os.getenv("SMARTMATCH_IMAGES_DIR")
    if not raw:
        raise RuntimeError(
            "SMARTMATCH_IMAGES_DIR must be set in the repository .env or passed via --image-root."
        )
    return Path(raw).expanduser()


def normalize_image_extension(value: str) -> str:
    extension = str(value or "").strip().lower().lstrip(".")
    if not EXTENSION_RE.fullmatch(extension):
        raise ValueError(f"Invalid image extension: {value!r}")
    return extension


@dataclass(frozen=True)
class SourceImageRef:
    source_path: str
    file_name: str
    file_extension: str | None
    target_file_name: str


@dataclass(frozen=True)
class TargetPathOccurrence:
    lost_artwork_id: Any
    source_path: str


def source_image_ref(path_value: str) -> SourceImageRef:
    parsed_path = urlparse(str(path_value)).path
    target_file_name = Path(parsed_path).name
    if not target_file_name or target_file_name in {".", ".."}:
        raise ValueError(f"Image path has no usable filename: {path_value!r}")
    suffix = Path(target_file_name).suffix
    if not suffix:
        return SourceImageRef(
            source_path=str(path_value),
            file_name=target_file_name,
            file_extension=None,
            target_file_name=target_file_name,
        )
    file_extension = normalize_image_extension(suffix.lstrip("."))
    file_name = target_file_name[: -len(suffix)]
    return SourceImageRef(
        source_path=str(path_value),
        file_name=file_name,
        file_extension=file_extension,
        target_file_name=target_file_name,
    )


def image_root_relative_path(image_root: Path, filename: str) -> str:
    return (image_root / filename).as_posix()


def validate_target_paths(image_root: Path, lost_rows: list[dict[str, Any]]) -> None:
    target_sources: dict[str, list[TargetPathOccurrence]] = {}
    for row in lost_rows:
        artwork_id = row.get("lost_artwork_id")
        for raw_path in list_value(row.get("img_paths")):
            if raw_path is None or not str(raw_path).strip():
                continue
            image = source_image_ref(str(raw_path))
            target_path = image_root_relative_path(image_root, image.target_file_name)
            target_sources.setdefault(target_path, []).append(
                TargetPathOccurrence(artwork_id, image.source_path)
            )

    collisions: list[tuple[str, list[TargetPathOccurrence]]] = []
    for target_path, occurrences in sorted(target_sources.items()):
        distinct_sources = {occurrence.source_path for occurrence in occurrences}
        if len(distinct_sources) > 1:
            collisions.append((target_path, occurrences))

    if not collisions:
        return

    lines = [
        "Refusing import because flattening files into SMARTMATCH_IMAGES_DIR would create "
        "conflicting target filenames:"
    ]
    for target_path, occurrences in collisions[:20]:
        lines.append(f"  {target_path}")
        for occurrence in occurrences[:5]:
            lines.append(
                "    "
                f"lost_artwork_id={occurrence.lost_artwork_id} "
                f"source_img_path={occurrence.source_path}"
            )
        if len(occurrences) > 5:
            lines.append(f"    ... {len(occurrences) - 5} more")
    if len(collisions) > 20:
        lines.append(f"  ... {len(collisions) - 20} more collisions")
    raise RuntimeError("\n".join(lines))


def load_existing_image_state(
    cur: psycopg.Cursor, artwork_ids: set[Any]
) -> dict[Any, dict[str, Any]]:
    if not artwork_ids:
        return {}
    cur.execute(
        "SELECT lost_artwork_id, img_paths FROM lost_artwork WHERE lost_artwork_id = ANY(%s)",
        (list(artwork_ids),),
    )
    state = {
        row["lost_artwork_id"]: {"img_paths": row.get("img_paths") or [], "linked_paths": {}}
        for row in cur.fetchall()
    }
    cur.execute(
        """
        SELECT laif.lost_artwork_id, img.image_file_id, img.file_path
        FROM lost_artwork_image_file laif
        JOIN image_file img ON img.image_file_id = laif.image_file_id
        WHERE laif.lost_artwork_id = ANY(%s)
        ORDER BY laif.lost_artwork_id, img.image_file_id
        """,
        (list(artwork_ids),),
    )
    for row in cur.fetchall():
        artwork_state = state.setdefault(
            row["lost_artwork_id"], {"img_paths": [], "linked_paths": {}}
        )
        file_path = str(row.get("file_path") or "").strip()
        if not file_path:
            continue
        artwork_state["linked_paths"].setdefault(file_path, []).append(row["image_file_id"])
    return state


def delete_existing_image_links(cur: psycopg.Cursor, artwork_id: Any) -> None:
    cur.execute(
        "SELECT image_file_id FROM lost_artwork_image_file WHERE lost_artwork_id = %s",
        (artwork_id,),
    )
    image_ids = [row["image_file_id"] for row in cur.fetchall()]
    if not image_ids:
        return
    cur.execute("DELETE FROM lost_artwork_image_file WHERE lost_artwork_id = %s", (artwork_id,))
    cur.execute(
        """
        DELETE FROM image_file img
        WHERE img.image_file_id = ANY(%s)
          AND NOT EXISTS (
              SELECT 1 FROM lost_artwork_image_file laif
              WHERE laif.image_file_id = img.image_file_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM auction_artwork_image_file aaif
              WHERE aaif.image_file_id = img.image_file_id
          )
        """,
        (image_ids,),
    )


def ensure_image_file_path_column(cur: psycopg.Cursor) -> None:
    cur.execute("ALTER TABLE image_file ADD COLUMN IF NOT EXISTS file_path text")


def allocate_image_file_ids(cur: psycopg.Cursor, count: int) -> list[int]:
    if count <= 0:
        return []
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
        (count,),
    )
    return [row["id"] for row in cur.fetchall()]


def insert_image_files(
    cur: psycopg.Cursor,
    image_ids: list[int],
    extensions: list[str | None],
    file_paths: list[str],
) -> None:
    cur.executemany(
        """
        INSERT INTO image_file (image_file_id, file_extension, file_path)
        VALUES (%s, %s, %s)
        """,
        list(zip(image_ids, extensions, file_paths, strict=True)),
    )


def update_image_files(
    cur: psycopg.Cursor,
    image_ids: list[int],
    extensions: list[str | None],
    file_paths: list[str],
) -> None:
    cur.executemany(
        """
        UPDATE image_file
        SET file_extension = %s,
            file_path = %s
        WHERE image_file_id = %s
        """,
        [
            (extension, file_path, image_id)
            for image_id, extension, file_path in zip(
                image_ids, extensions, file_paths, strict=True
            )
        ],
    )


def allocate_images(
    cur: psycopg.Cursor,
    *,
    artwork_id: Any,
    lost_art_id: str | None,
    source_artwork_id: Any,
    source_paths: list[str],
    existing_state: dict[Any, dict[str, Any]],
    image_root: Path,
) -> tuple[list[str], list[dict[str, Any]], list[tuple[Any, int]]]:
    if not source_paths:
        return [], [], []
    source_images = [source_image_ref(path) for path in source_paths]
    db_paths = [
        image_root_relative_path(image_root, image.target_file_name)
        for image in source_images
    ]
    extensions = [image.file_extension for image in source_images]
    state = existing_state.get(artwork_id, {"img_paths": [], "linked_paths": {}})
    linked_paths = {
        file_path: list(image_ids)
        for file_path, image_ids in state.get("linked_paths", {}).items()
    }
    reusable_image_ids: list[int] = []
    can_reuse = state.get("img_paths", []) == db_paths
    if can_reuse:
        try:
            for file_path in db_paths:
                reusable_image_ids.append(linked_paths[file_path].pop(0))
        except (KeyError, IndexError):
            can_reuse = False
            reusable_image_ids = []

    ensure_image_file_path_column(cur)
    image_ids: list[int]
    reused = False
    if can_reuse:
        image_ids = reusable_image_ids
        reused = True
        update_image_files(cur, image_ids, extensions, db_paths)
    else:
        delete_existing_image_links(cur, artwork_id)
        image_ids = allocate_image_file_ids(cur, len(source_images))
        insert_image_files(cur, image_ids, extensions, db_paths)
    manifest_rows = [
        {
            "source_lost_artwork_id": source_artwork_id,
            "target_lost_artwork_id": artwork_id,
            "lost_art_id": lost_art_id or "",
            "source_img_path": image.source_path,
            "target_img_path": target_path,
            "image_file_id": image_id,
            "file_extension": image.file_extension or "",
            "reused_existing": str(reused).lower(),
        }
        for image, target_path, image_id in zip(source_images, db_paths, image_ids, strict=True)
    ]
    junctions = [(artwork_id, image_id) for image_id in image_ids]
    return db_paths, manifest_rows, junctions


def jsonb_value(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            return Jsonb(json.loads(value))
        except json.JSONDecodeError:
            return Jsonb(value)
    return Jsonb(value)


def text_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def dict_ids_from_row(
    row: dict[str, Any], id_key: str, name_key: str, name_map: dict[str, UUID]
) -> list[Any]:
    ids = list_value(row.get(id_key))
    if ids:
        return ids
    return [name_map[str(name)] for name in list_value(row.get(name_key)) if str(name) in name_map]


def existing_by_lost_art_id(
    cur: psycopg.Cursor, lost_rows: list[dict[str, Any]]
) -> dict[Any, Any]:
    lost_art_ids = sorted({row.get("lost_art_id") for row in lost_rows if row.get("lost_art_id")})
    if not lost_art_ids:
        return {}
    cur.execute(
        """
        SELECT lost_art_id, array_agg(lost_artwork_id) AS ids
        FROM lost_artwork
        WHERE lost_art_id = ANY(%s)
        GROUP BY lost_art_id
        """,
        (lost_art_ids,),
    )
    by_lost_art_id: dict[str, Any] = {}
    for row in cur.fetchall():
        ids = row["ids"] or []
        if len(ids) > 1:
            raise RuntimeError(
                f"Production has multiple lost_artwork rows for lost_art_id={row['lost_art_id']!r}"
            )
        by_lost_art_id[row["lost_art_id"]] = ids[0]
    return {
        row["lost_artwork_id"]: by_lost_art_id[row["lost_art_id"]]
        for row in lost_rows
        if row.get("lost_art_id") in by_lost_art_id
    }


def remap_id_list(value: Any, mapping: dict[Any, Any]) -> list[Any]:
    return [mapping.get(item, item) for item in list_value(value)]


def convert_lost_row(
    row: dict[str, Any],
    *,
    target_artwork_id: Any,
    img_paths: list[str],
    artist_id_map: dict[Any, Any],
    material_name_map: dict[str, UUID],
    technique_name_map: dict[str, UUID],
) -> dict[str, Any]:
    return {
        "lost_artwork_id": target_artwork_id,
        "title": row.get("title") or "",
        "title_en": row.get("title_en"),
        "artist_ids": remap_id_list(row.get("artist_ids"), artist_id_map),
        "depicted_person_ids": remap_id_list(row.get("depicted_person_ids"), artist_id_map),
        "img_paths": img_paths,
        "width": row.get("width"),
        "height": row.get("height"),
        "width_frame": row.get("width_frame"),
        "height_frame": row.get("height_frame"),
        "depth": text_value(row.get("depth")),
        "diameter": text_value(row.get("diameter")),
        "dating": row.get("dating"),
        "dating_start": row.get("dating_start"),
        "dating_end": row.get("dating_end"),
        "description": row.get("description"),
        "description_en": row.get("description_en"),
        "inventory_number": row.get("inventory_number"),
        "material": row.get("material"),
        "dict_material_id": dict_ids_from_row(
            row, "dict_material_id", "dict_material_name", material_name_map
        ),
        "material_en": row.get("material_en"),
        "technique": row.get("technique"),
        "dict_technique_id": dict_ids_from_row(
            row, "dict_technique_id", "dict_technique_name", technique_name_map
        ),
        "technique_en": row.get("technique_en"),
        "provenance": row.get("provenance"),
        "provenance_en": row.get("provenance_en"),
        "institution_id": row.get("institution_id"),
        "circumstances_of_loss": row.get("circumstances_of_loss"),
        "lost_art_id": row.get("lost_art_id"),
        "lost_art_url": row.get("lost_art_url"),
        "keywords": list_value(row.get("keywords")),
        "literature_source_id": row.get("literature_source_id"),
        "literature_source_page": row.get("literature_source_page"),
        "type_of_loss": row.get("type_of_loss"),
        "restituted": bool(row.get("restituted")) if row.get("restituted") is not None else False,
        "raw_data": jsonb_value(row.get("raw_data")),
        "location_history": jsonb_value(row.get("location_history")),
    }


def upsert_lost_artworks(cur: psycopg.Cursor, rows: list[dict[str, Any]]) -> None:
    assignments = [col for col in TARGET_LOST_COLUMNS if col != "lost_artwork_id"]
    query = sql.SQL(
        "INSERT INTO lost_artwork ({cols}) VALUES ({values}) "
        "ON CONFLICT (lost_artwork_id) DO UPDATE SET {assignments}"
    ).format(
        cols=sql.SQL(", ").join(map(sql.Identifier, TARGET_LOST_COLUMNS)),
        values=sql.SQL(", ").join(sql.Placeholder() for _ in TARGET_LOST_COLUMNS),
        assignments=sql.SQL(", ").join(
            sql.SQL("{col} = EXCLUDED.{col}").format(col=sql.Identifier(col))
            for col in assignments
        ),
    )
    cur.executemany(query, ([row[col] for col in TARGET_LOST_COLUMNS] for row in rows))


def insert_junctions(cur: psycopg.Cursor, junctions: list[tuple[Any, int]]) -> None:
    if not junctions:
        return
    cur.executemany(
        """
        INSERT INTO lost_artwork_image_file (lost_artwork_id, image_file_id)
        VALUES (%s, %s)
        ON CONFLICT (lost_artwork_id, image_file_id) DO NOTHING
        """,
        junctions,
    )


def manifest_path(args: argparse.Namespace) -> Path:
    if args.manifest:
        return Path(args.manifest).expanduser()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_MANIFEST_DIR / f"lost_artwork_image_copy_manifest_{timestamp}.csv"


def write_manifest(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "source_lost_artwork_id",
        "target_lost_artwork_id",
        "lost_art_id",
        "source_img_path",
        "target_img_path",
        "image_file_id",
        "file_extension",
        "reused_existing",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_import(args: argparse.Namespace, prod_cfg: DbConfig, nonprod_cfg: DbConfig) -> int:
    image_root = image_root_from_args(args)
    manifest_rows: list[dict[str, Any]] = []
    with connect(nonprod_cfg) as source, connect(prod_cfg) as target:
        lost_rows, source_columns = fetch_lost_rows(
            source,
            only_lostart=args.only_lostart,
            lost_art_ids=args.lost_art_id,
            limit=args.limit,
        )
        if not lost_rows:
            print("No source lost_artwork rows matched the requested filters.")
            return 0
        print(f"Loaded {len(lost_rows)} source lost_artwork rows ({len(source_columns)} columns).")
        validate_target_paths(image_root, lost_rows)

        try:
            with target.transaction():
                with target.cursor() as cur:
                    target_id_map = (
                        {}
                        if args.no_match_by_lost_art_id
                        else existing_by_lost_art_id(cur, lost_rows)
                    )
                    support_counts, artist_id_map = copy_supporting_rows(
                        source, cur, lost_rows
                    )
                    material_map, technique_map = prepare_dictionaries(source, cur, lost_rows)
                    target_ids_in_order = [
                        target_id_map.get(row["lost_artwork_id"], row["lost_artwork_id"])
                        for row in lost_rows
                    ]
                    if len(set(target_ids_in_order)) != len(target_ids_in_order):
                        raise RuntimeError(
                            "Multiple source rows map to the same production lost_artwork_id; "
                            "deduplicate the source rows or rerun with --no-match-by-lost-art-id."
                        )
                    image_state = load_existing_image_state(cur, set(target_ids_in_order))

                    converted_rows: list[dict[str, Any]] = []
                    junctions: list[tuple[Any, int]] = []
                    for row in lost_rows:
                        source_artwork_id = row["lost_artwork_id"]
                        target_artwork_id = target_id_map.get(
                            source_artwork_id, source_artwork_id
                        )
                        source_paths = [str(path) for path in list_value(row.get("img_paths")) if path]
                        db_paths, image_manifest, image_junctions = allocate_images(
                            cur,
                            artwork_id=target_artwork_id,
                            lost_art_id=row.get("lost_art_id"),
                            source_artwork_id=source_artwork_id,
                            source_paths=source_paths,
                            existing_state=image_state,
                            image_root=image_root,
                        )
                        manifest_rows.extend(image_manifest)
                        junctions.extend(image_junctions)
                        converted_rows.append(
                            convert_lost_row(
                                row,
                                target_artwork_id=target_artwork_id,
                                img_paths=db_paths,
                                artist_id_map=artist_id_map,
                                material_name_map=material_map,
                                technique_name_map=technique_map,
                            )
                        )

                    upsert_lost_artworks(cur, converted_rows)
                    insert_junctions(cur, junctions)

                    print("Copied supporting rows:")
                    for name, count in support_counts.items():
                        print(f"  {name}: {count}")
                    print(f"Upserted lost_artwork rows: {len(converted_rows)}")
                    print(f"Created/reused image_file links: {len(junctions)}")
                    print(f"Matched existing production rows by lost_art_id: {len(target_id_map)}")
                    if args.dry_run:
                        raise DryRunRollback()
        except DryRunRollback:
            print("Dry run complete; production transaction rolled back.")
            return 0

    if manifest_rows and not args.no_manifest:
        path = manifest_path(args)
        write_manifest(path, manifest_rows)
        print(f"Wrote image copy manifest: {path}")
    print("Import committed successfully.")
    return 0


def main() -> int:
    args = parse_args()
    load_env_file(Path(args.env_file))
    prod_cfg = db_config("")
    nonprod_cfg = db_config("NON_PROD_")
    if same_db(prod_cfg, nonprod_cfg) and not args.allow_same_db:
        raise RuntimeError("Production and non-production DB configs point to the same DB.")
    if not args.dry_run and not args.skip_backup:
        create_backup(prod_cfg, DEFAULT_BACKUP_DIR)
    return run_import(args, prod_cfg, nonprod_cfg)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
