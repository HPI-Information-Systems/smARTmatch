#!/usr/bin/env python3
"""Generate SQLAlchemy ORM models from the Postgres schema SQL.

Usage
  python3 scrapers/generate_models.py
  python3 scrapers/generate_models.py --schema db/init-production/01_schema_production.sql --out scrapers/models_production.py

Notes
- The generated file imports SQLAlchemy, but this generator does not.
- Relationships are generated with simple naming heuristics.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "db"
    / "init-production"
    / "01_schema_production.sql"
)
DEFAULT_OUT_PATH = Path(__file__).resolve().parent / "models_production.py"

# TODO: Keep ORM output aligned with production
COLUMN_TYPE_OVERRIDES: dict[tuple[str, str], str] = {
    ("auction_artwork", "title"): "text",
    ("auction_artwork", "title_en"): "text",
    ("auction_artwork", "lot_url"): "text",
    ("auction_artwork", "auction_details"): "jsonb",
    ("auction_artwork", "raw_data"): "jsonb",
    ("lost_artwork", "depth"): "text",
    ("lost_artwork", "diameter"): "text",
    ("lost_artwork", "raw_data"): "jsonb",
}


@dataclass(frozen=True)
class ForeignKeySpec:
    ref_table: str
    ref_column: str
    ondelete: Optional[str]


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    sql_type: str
    nullable: bool
    primary_key: bool
    unique: bool
    default: Optional[str]
    foreign_key: Optional[ForeignKeySpec]
    check: Optional[str]


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: list[ColumnSpec]
    uniques: list[list[str]]


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _strip_sql_comments(sql_text: str) -> str:
    lines: list[str] = []
    for line in sql_text.splitlines():
        # remove -- comments
        if "--" in line:
            line = line.split("--", 1)[0]
        lines.append(line)
    return "\n".join(lines)


def _split_top_level_csv(block: str) -> list[str]:
    """Split a CREATE TABLE (...) block into items by commas at paren-depth 0."""
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    in_single_quote = False

    i = 0
    while i < len(block):
        ch = block[i]
        if ch == "'":
            # toggle single quotes; handle escaped ''
            if i + 1 < len(block) and block[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_single_quote = not in_single_quote
            buf.append(ch)
            i += 1
            continue

        if not in_single_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                item = "".join(buf).strip()
                if item:
                    items.append(item)
                buf = []
                i += 1
                continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _iter_create_table_blocks(sql_text: str) -> Iterator[tuple[str, str]]:
    """Yield (table_name, inner_block_text) for each CREATE TABLE statement."""
    cleaned = _strip_sql_comments(sql_text)
    # A simple regex-based scanner for this repo's DDL style.
    pattern = re.compile(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<body>.*?)\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(cleaned):
        yield match.group("name"), match.group("body")


def _parse_foreign_key(tokens: str) -> Optional[ForeignKeySpec]:
    m = re.search(
        r"REFERENCES\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)\s*\(\s*(?P<col>[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*(?:ON\s+DELETE\s+(?P<ondelete>CASCADE|SET\s+NULL|RESTRICT|NO\s+ACTION))?",
        tokens,
        re.IGNORECASE,
    )
    if not m:
        return None
    ondelete = m.group("ondelete")
    if ondelete:
        ondelete = " ".join(ondelete.upper().split())
        # SQLAlchemy expects e.g. 'SET NULL'
    return ForeignKeySpec(
        ref_table=m.group("table"),
        ref_column=m.group("col"),
        ondelete=ondelete,
    )


def _parse_column(item: str) -> Optional[ColumnSpec]:
    # table constraints
    if re.match(
        r"^(CONSTRAINT|UNIQUE|PRIMARY\s+KEY|FOREIGN\s+KEY|CHECK)\b",
        item.strip(),
        re.IGNORECASE,
    ):
        return None

    # name + type are first two tokens (type may have parens e.g. varchar(255))
    parts = item.strip().split(None, 2)
    if len(parts) < 2:
        return None
    col_name, col_type = parts[0], parts[1]

    if not _IDENTIFIER_RE.match(col_name):
        return None

    nullable = True
    primary_key = False
    unique = bool(re.search(r"\bUNIQUE\b", item, re.IGNORECASE))

    if re.search(r"\bNOT\s+NULL\b", item, re.IGNORECASE):
        nullable = False
    if re.search(r"\bPRIMARY\s+KEY\b", item, re.IGNORECASE):
        primary_key = True
        nullable = False

    default: Optional[str] = None
    m_def = re.search(
        r"\bDEFAULT\s+(?P<default>[^\s,]+(?:\s*\([^)]*\))?)", item, re.IGNORECASE
    )
    if m_def:
        default = m_def.group("default").strip()

    fk = _parse_foreign_key(item)

    check: Optional[str] = None
    m_check = re.search(r"\bCHECK\s*\((?P<expr>[^)]*)\)", item, re.IGNORECASE)
    if m_check:
        check = m_check.group("expr").strip()

    return ColumnSpec(
        name=col_name,
        sql_type=col_type,
        nullable=nullable,
        primary_key=primary_key,
        unique=unique,
        default=default,
        foreign_key=fk,
        check=check,
    )


def _parse_uniques(items: Iterable[str]) -> list[list[str]]:
    uniques: list[list[str]] = []
    for item in items:
        m = re.match(r"^UNIQUE\s*\((?P<cols>[^)]*)\)\s*$", item.strip(), re.IGNORECASE)
        if not m:
            continue
        cols = [c.strip() for c in m.group("cols").split(",") if c.strip()]
        if cols:
            uniques.append(cols)
    return uniques


def _parse_table_primary_key(items: Iterable[str]) -> set[str]:
    for item in items:
        m = re.match(
            r"^PRIMARY\s+KEY\s*\((?P<cols>[^)]*)\)\s*$",
            item.strip(),
            re.IGNORECASE,
        )
        if m:
            return {c.strip() for c in m.group("cols").split(",") if c.strip()}
    return set()


def _apply_table_primary_keys(cols: list[ColumnSpec], pk_cols: set[str]) -> list[ColumnSpec]:
    if not pk_cols:
        return cols
    return [
        ColumnSpec(
            name=col.name,
            sql_type=col.sql_type,
            nullable=False if col.name in pk_cols else col.nullable,
            primary_key=True if col.name in pk_cols else col.primary_key,
            unique=col.unique,
            default=col.default,
            foreign_key=col.foreign_key,
            check=col.check,
        )
        for col in cols
    ]


def parse_schema(schema_path: Path) -> list[TableSpec]:
    sql_text = schema_path.read_text(encoding="utf-8")
    tables: list[TableSpec] = []

    for table_name, body in _iter_create_table_blocks(sql_text):
        items = _split_top_level_csv(body)
        cols: list[ColumnSpec] = []
        for item in items:
            col = _parse_column(item)
            if col is not None:
                cols.append(col)
        cols = _apply_table_primary_keys(cols, _parse_table_primary_key(items))
        tables.append(
            TableSpec(name=table_name, columns=cols, uniques=_parse_uniques(items))
        )

    # deterministic order
    tables.sort(key=lambda t: t.name)
    return tables


def _snake_to_camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _sqlalchemy_type(*, table_name: str, column_name: str, sql_type: str) -> str:
    t = COLUMN_TYPE_OVERRIDES.get((table_name, column_name), sql_type).lower()

    # arrays
    if t.endswith("[]"):
        base = t[:-2]
        if base == "uuid":
            return "ARRAY(UUID(as_uuid=True))"
        if base == "text":
            return "ARRAY(Text)"
        return "ARRAY(Text)"

    if t.startswith("vector"):
        m = re.search(r"\((\d+)\)", t)
        size = m.group(1) if m else "None"
        return f"Vector({size})"
    if t == "uuid":
        return "UUID(as_uuid=True)"
    if t.startswith("varchar"):
        m = re.search(r"\((\d+)\)", t)
        size = m.group(1) if m else "255"
        return f"String({size})"
    if t == "text":
        return "Text"
    if t == "real":
        return "Float"
    if t == "date":
        return "Date"
    if t in ("timestamptz", "timestampz"):
        return "TIMESTAMP(timezone=True)"
    if t == "serial":
        return "Integer"
    if t == "integer":
        return "Integer"
    if t == "bigint":
        return "BigInteger"
    if t == "boolean":
        return "Boolean"
    if t == "smallint":
        return "SmallInteger"
    if t in ("json", "jsonb"):
        return "JSON"

    # fallback
    return "Text"


def _python_default(col: ColumnSpec) -> Optional[str]:
    if col.default is None:
        return None

    d = col.default.strip().lower()

    # UUID defaults
    if d.startswith("gen_random_uuid"):
        return "uuid4"

    # empty object defaults
    if d.startswith("'{}'::json"):
        return "dict"

    # arrays with '{}'::text[]
    if d.startswith("'{}'::"):
        return "list"

    if d in ("false", "true"):
        return d.capitalize()

    if d.startswith("now(") or d == "now()":
        return "datetime.utcnow"

    if re.fullmatch(r"-?\d+", d):
        return d

    return None


def _relationship_attr(child_table: str, col: ColumnSpec) -> Optional[str]:
    if not col.foreign_key:
        return None

    # small special-case for artist.place_of_birth/place_of_death -> location
    if col.foreign_key.ref_table == "location" and col.name == "place_of_birth":
        return "birth_location"
    if col.foreign_key.ref_table == "location" and col.name == "place_of_death":
        return "death_location"

    if col.name.endswith("_id"):
        return col.name[: -len("_id")]

    # fallback
    return f"{col.name}_obj"


def _collection_attr(child_table: str, rel_attr: str, *, disambiguate: bool) -> str:
    # naive pluralization: add 's' unless already ends in 's'
    base = child_table if child_table.endswith("s") else f"{child_table}s"
    if not disambiguate:
        return base
    # If the parent is referenced multiple times from the same child table,
    # include the relationship attribute to avoid duplicate names.
    return f"{base}_via_{rel_attr}"


def render_models(tables: list[TableSpec]) -> str:
    # Gather all foreign keys for relationships
    fk_by_child: dict[str, list[tuple[str, ColumnSpec]]] = {}
    children_by_parent: dict[str, list[tuple[str, str, ColumnSpec]]] = {}

    for table in tables:
        for col in table.columns:
            if not col.foreign_key:
                continue
            rel_attr = _relationship_attr(table.name, col)
            if not rel_attr:
                continue
            fk_by_child.setdefault(table.name, []).append((rel_attr, col))
            children_by_parent.setdefault(col.foreign_key.ref_table, []).append(
                (table.name, rel_attr, col)
            )

    # Imports
    out: list[str] = []
    out.append('"""SQLAlchemy ORM models for smARTmatch."""')
    out.append("")
    out.append("# This file is generated by scrapers/generate_models.py")
    out.append(
        "# Do not edit manually; edit db/init-production/01_schema_production.sql (and"
    )
    out.append(
        "# scrapers/generate_models.py for production hardening), then re-run the generator."
    )
    out.append("")
    out.append("from __future__ import annotations")
    out.append("")
    out.append("from datetime import date, datetime")
    out.append("from typing import List, Optional")
    out.append("from uuid import uuid4")
    out.append("")
    out.append(
        "from sqlalchemy import ARRAY, JSON, BigInteger, Boolean, Date, Float, ForeignKey, Integer, SmallInteger, String, Text"
    )
    if any(table.uniques for table in tables):
        out.append("from sqlalchemy import UniqueConstraint")
    out.append("from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID")
    out.append(
        "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship"
    )
    out.append("")
    out.append("")
    out.append("class Base(DeclarativeBase):")
    out.append("    pass")

    # Classes
    for table in tables:
        class_name = _snake_to_camel(table.name)
        out.append("")
        out.append("")
        out.append(f"class {class_name}(Base):")
        out.append(f'    __tablename__ = "{table.name}"')

        if table.uniques:
            # Convert to UniqueConstraint entries
            constraints = ", ".join(
                "UniqueConstraint(" + ", ".join(repr(c) for c in cols) + ")"
                for cols in table.uniques
            )
            out.append(f"    __table_args__ = ({constraints},)")

        out.append("")

        # Columns
        for col in table.columns:
            py_type = (
                "UUID" if col.sql_type.lower().startswith("uuid") else "Optional[str]"
            )
            # better typing based on mapped types
            sqlatype = _sqlalchemy_type(
                table_name=table.name,
                column_name=col.name,
                sql_type=col.sql_type,
            )
            if sqlatype.startswith("String"):
                py_type = "str" if not col.nullable else "Optional[str]"
            elif sqlatype == "Text":
                py_type = "str" if not col.nullable else "Optional[str]"
            elif sqlatype in {"Integer", "BigInteger"}:
                py_type = "int" if not col.nullable else "Optional[int]"
            elif sqlatype == "Float":
                py_type = "float" if not col.nullable else "Optional[float]"
            elif sqlatype == "Date":
                py_type = "date" if not col.nullable else "Optional[date]"
            elif sqlatype == "Boolean":
                py_type = "bool" if not col.nullable else "Optional[bool]"
            elif sqlatype == "SmallInteger":
                py_type = "int" if not col.nullable else "Optional[int]"
            elif sqlatype == "JSON":
                py_type = "dict" if not col.nullable else "Optional[dict]"
            elif sqlatype.startswith("TIMESTAMP"):
                py_type = "datetime" if not col.nullable else "Optional[datetime]"
            elif sqlatype.startswith("UUID"):
                py_type = "UUID" if not col.nullable else "Optional[UUID]"
            elif sqlatype.startswith("ARRAY"):
                # arrays are lists
                if "UUID" in sqlatype:
                    py_type = "List[UUID]" if not col.nullable else "List[UUID]"
                else:
                    py_type = "List[str]" if not col.nullable else "List[str]"
            elif sqlatype.startswith("Vector"):
                py_type = "List[float]" if not col.nullable else "Optional[List[float]]"

            args: list[str] = [sqlatype]
            kwargs: list[str] = []
            if col.foreign_key:
                fk = col.foreign_key
                fk_target = f"{fk.ref_table}.{fk.ref_column}"
                if fk.ondelete:
                    kwargs.append(
                        f'ForeignKey("{fk_target}", ondelete="{fk.ondelete}")'
                    )
                else:
                    kwargs.append(f'ForeignKey("{fk_target}")')
                # When using ForeignKey object as the first arg after type, place it as arg
                args.append(kwargs.pop())

            if col.primary_key:
                kwargs.append("primary_key=True")
            if col.unique:
                kwargs.append("unique=True")
            if not col.nullable and not col.primary_key:
                kwargs.append("nullable=False")

            py_def = _python_default(col)
            if py_def == "uuid4":
                kwargs.append("default=uuid4")
            elif py_def == "datetime.utcnow":
                kwargs.append("default=datetime.utcnow")
            elif py_def == "list":
                kwargs.append("default=list")
            elif py_def == "dict":
                kwargs.append("default=dict")
            elif py_def in ("False", "True"):
                kwargs.append(f"default={py_def}")
            elif py_def is not None and re.fullmatch(r"-?\d+", py_def):
                kwargs.append(f"default={py_def}")

            # Build mapped_column(...)
            arg_str = ", ".join(args + kwargs)
            out.append(f"    {col.name}: Mapped[{py_type}] = mapped_column({arg_str})")

        # Relationships (child -> parent)
        rels = fk_by_child.get(table.name, [])
        if rels:
            out.append("")
            for rel_attr, col in rels:
                fk = col.foreign_key
                assert fk is not None
                parent_class = _snake_to_camel(fk.ref_table)
                optional = "Optional" if col.nullable else "Optional"
                # always Optional since FK can be NULL in schema except explicit NOT NULL
                rel_type = f'{optional}["{parent_class}"]'
                # foreign_keys arg helps when multiple FKs to same table
                out.append(
                    f'    {rel_attr}: Mapped[{rel_type}] = relationship("{parent_class}", foreign_keys=[{col.name}])'
                )

        # Relationships (parent -> children collections)
        child_rels = children_by_parent.get(table.name, [])
        if child_rels:
            out.append("")
            # Count how often each child_table appears for disambiguation.
            child_counts: dict[str, int] = {}
            for child_table, _rel_attr, _col in child_rels:
                child_counts[child_table] = child_counts.get(child_table, 0) + 1

            for child_table, _rel_attr, _col in sorted(
                child_rels, key=lambda x: (x[0], x[1])
            ):
                child_class = _snake_to_camel(child_table)
                coll_attr = _collection_attr(
                    child_table,
                    _rel_attr,
                    disambiguate=child_counts.get(child_table, 0) > 1,
                )
                fk_arg = ""
                if child_counts.get(child_table, 0) > 1:
                    fk_arg = f', foreign_keys="[{child_class}.{_col.name}]"'
                # Provide overlaps to silence SQLAlchemy configure_mappers warnings
                overlaps_arg = f', overlaps="{_rel_attr}"'
                out.append(
                    f'    {coll_attr}: Mapped[List["{child_class}"]] = relationship("{child_class}"{fk_arg}{overlaps_arg})'
                )

    out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate SQLAlchemy models.py from schema SQL"
    )
    parser.add_argument(
        "--schema", type=Path, default=DEFAULT_SCHEMA_PATH, help="Path to schema SQL"
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT_PATH, help="Path to write models.py"
    )
    parser.add_argument(
        "--stdout", action="store_true", help="Print output instead of writing file"
    )

    args = parser.parse_args()

    if not args.schema.exists():
        raise SystemExit(f"Schema not found: {args.schema}")

    tables = parse_schema(args.schema)
    rendered = render_models(tables)

    if args.stdout:
        print(rendered)
        return 0

    args.out.write_text(rendered, encoding="utf-8")
    print(f"Wrote {args.out} from {args.schema}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
