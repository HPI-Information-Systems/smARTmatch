"""Shared lookup logic for material/technique dictionary normalization.

This module is intentionally DB-read-only. It loads variant tables once and then
normalizes raw material/technique strings into dict_* array values in memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

import psycopg

_SPLIT_PATTERN = re.compile(
    r"[/,;()]+|\b(?:and|und|mit|over|et|auf|über)\b",
    flags=re.IGNORECASE,
)
_IGNORED_TOKENS = {"and", "und", "mit", "over", "et", "auf", "über"}

#the class that holds the variant maps and root names for material/technique normalization
@dataclass(frozen=True)
class TechniqueMaterialDictContext:
    material_variants: dict[str, list[str]]
    technique_variants: dict[str, list[str]]
    material_root: str
    technique_root: str


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

# this function deduplicates and keeps the order of the values in the list.
def _dedupe_keep_order(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def split_raw_data(raw_text: Any) -> list[str]:
    """Split raw material/technique text into a list of cleaned, lowercased parts/tokens."""
    text = _clean_text(raw_text)
    if text is None:
        return []

    parts: list[str] = []
    for token in _SPLIT_PATTERN.split(text):
        cleaned = token.strip().lower()
        if cleaned and cleaned not in _IGNORED_TOKENS:
            parts.append(cleaned)

    return _dedupe_keep_order(parts)

# this function creates a mapping of raw material/technique values to their corresponding normalized dictionary names. 
def _load_variant_map(
    cur: psycopg.Cursor,
    table_name: str,
    dict_col: str,
    raw_col: str,
) -> dict[str, list[str]]:
    cur.execute(f"SELECT {dict_col}, {raw_col} FROM {table_name}")
    variants: dict[str, list[str]] = {}

    for dict_name, raw_data in cur.fetchall():
        raw_key = _clean_text(raw_data)
        dict_value = _clean_text(dict_name)
        if raw_key is None or dict_value is None:
            continue

        key = raw_key.lower()
        current = variants.setdefault(key, [])
        if dict_value not in current:
            current.append(dict_value)

    return variants

# This function searches for the root name of a material/technique in the database. 
def _load_root_name(
    cur: psycopg.Cursor,
    table_name: str,
    name_col: str,
    parent_col: str,
    fallback: str,
) -> str:
    cur.execute(
        f"""
        SELECT {name_col}
        FROM {table_name}
        WHERE {parent_col} IS NULL
        ORDER BY {name_col}
        LIMIT 1
        """
    )
    row = cur.fetchone()
    return str(row[0]) if row and row[0] else fallback

# this function call s the _load_variant_map and _load_root_name functions to load the variant maps and root names for material/technique normalization from the database. 
def load_dict_context(conn: psycopg.Connection) -> TechniqueMaterialDictContext:
    """Load material/technique variant maps from DB once."""
    with conn.cursor() as cur:
        material_root = _load_root_name(
            cur,
            table_name="dict_material",
            name_col="material_name",
            parent_col="material_parent",
            fallback="unbekannt",
        )
        technique_root = _load_root_name(
            cur,
            table_name="dict_technique",
            name_col="technique_name",
            parent_col="technique_parent",
            fallback="Unbekannt",
        )
        material_variants = _load_variant_map(
            cur,
            table_name="material_variant",
            dict_col="dict_material_name",
            raw_col="material_raw_data",
        )
        technique_variants = _load_variant_map(
            cur,
            table_name="technique_variant",
            dict_col="dict_technique_name",
            raw_col="technique_raw_data",
        )

    return TechniqueMaterialDictContext(
        material_variants=material_variants,
        technique_variants=technique_variants,
        material_root=material_root,
        technique_root=technique_root,
    )


def resolve_dict_names(
    raw_text: Any,
    variants: dict[str, list[str]],
    root_name: str,
    *,
    fallback_to_root: bool = True,
) -> list[str]:
    """Resolve one raw material/technique field to normalized dictionary names.

    Empty raw input returns [root_name] by default. Non-empty raw input with no
    variant hit also returns [root_name], so normalization always produces a
    deterministic dictionary array when fallback_to_root=True.
    """
    # if the raw_text is None or empty, return the root_name ("unbekannt") as a fallback
    parts = split_raw_data(raw_text)
    if not parts:
        return [root_name] if fallback_to_root else []
    
    # iterate over the parts of the raw_text and look up their corresponding normalized dictionary names in the variants mapping. 
    found: list[str] = []
    for part in parts:
        found.extend(variants.get(part, []))
        
    #deduplicate the found list and keep the order of the values in the list.
    found = _dedupe_keep_order(found)
    if found:
        return found

    return [root_name] if fallback_to_root else []


def resolve_material_dict_names(
    raw_material: Any,
    context: TechniqueMaterialDictContext,
    *,
    fallback_to_root: bool = True,
) -> list[str]:
    return resolve_dict_names(
        raw_material,
        context.material_variants,
        context.material_root,
        fallback_to_root=fallback_to_root,
    )


def resolve_technique_dict_names(
    raw_technique: Any,
    context: TechniqueMaterialDictContext,
    *,
    fallback_to_root: bool = True,
) -> list[str]:
    return resolve_dict_names(
        raw_technique,
        context.technique_variants,
        context.technique_root,
        fallback_to_root=fallback_to_root,
    )


def normalize_existing_array(value: Any) -> list[str]:
    """Normalize a Python/PG-style array value into a list of strings."""
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        return _dedupe_keep_order(str(item) for item in value if item is not None)

    text = str(value).strip()
    if not text:
        return []

    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]

    if "," in text:
        return _dedupe_keep_order(part.strip().strip('"') for part in text.split(","))

    return [text.strip('"')]
