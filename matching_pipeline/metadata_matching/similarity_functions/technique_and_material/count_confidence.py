"""
This module provides confidence scoring for material and technique hierarchy values.

The score is derived from the distance to the root in the hierarchy tree.
Deeper nodes are considered more specific and therefore more confident.
"""

from __future__ import annotations
import matching_pipeline.metadata_matching.similarity_functions.technique_and_material.load_hierarchy as hierarchy

def _ensure_loaded() -> None:
    if not hierarchy.IS_LOADED:
        hierarchy.load_hierarchies()

def _normalize_name(name: object) -> str:
    return str(name).strip()

def _extract_names(value) -> list[str]:
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        return [_normalize_name(item) for item in value if item is not None and str(item).strip()]

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []

        if stripped.startswith("{") and stripped.endswith("}"):
            stripped = stripped[1:-1]

        for separator in [",", ";", "|"]:
            if separator in stripped:
                return [_normalize_name(part) for part in stripped.split(separator) if part.strip()]

        return [_normalize_name(stripped)]

    return [_normalize_name(value)]


def _root_name(hierarchy_type: str) -> str:
    return hierarchy.ROOT_NAME_MATERIAL if hierarchy_type == "material" else hierarchy.ROOT_NAME_TECHNIQUE

# compute the maximum depth of the hierarchy tree for a given type (material or technique)
def _hierarchy_height(hierarchy_type: str) -> int:
    _ensure_loaded()

    paths = (
        hierarchy.MATERIAL_PATHS_TO_ROOT
        if hierarchy_type == "material"
        else hierarchy.TECHNIQUE_PATHS_TO_ROOT
    )

    if not paths:
        return 0

    return max(max(0, len(path) - 1) for path in paths.values())

# calculate the ratio of a node's depth to the maximum depth in the hierarchy
def _node_depth_ratio(name: str, hierarchy_type: str) -> float:
    _ensure_loaded()

    if not name:
        return 0.0

    root_name = _root_name(hierarchy_type)
    if name.lower() == root_name.lower():
        return 0.0

    path = hierarchy.get_path_to_root(name, hierarchy_type)
    if not path or len(path) <= 1:
        return 0.0

    max_depth = _hierarchy_height(hierarchy_type)
    if max_depth <= 0:
        return 0.0

    depth = len(path) - 1
    return max(0.0, min(1.0, depth / max_depth))


def _field_confidence(value, hierarchy_type: str) -> float:
    names = _extract_names(value)
    if not names:
        return 0.0

    scores = [_node_depth_ratio(name, hierarchy_type) for name in names]
    return sum(scores) / len(scores)

def _is_missing_or_root(value, hierarchy_type: str) -> bool:
    names = _extract_names(value)
    if not names:
        return True

    root_name = _root_name(hierarchy_type).lower()
    return all(name.lower() == root_name for name in names)

def confidence_function(
    lost_dict_material,
    auction_dict_material,
    lost_dict_technique,
    auction_dict_technique,
) -> tuple[float, float]:

    if _is_missing_or_root(lost_dict_material, "material") or _is_missing_or_root(auction_dict_material, "material"):
        material_score = 0.0
    else:
        material_score = (
            _field_confidence(lost_dict_material, "material")
            + _field_confidence(auction_dict_material, "material")
        ) / 2.0

    if _is_missing_or_root(lost_dict_technique, "technique") or _is_missing_or_root(auction_dict_technique, "technique"):
        technique_score = 0.0
    else:
        technique_score = (
            _field_confidence(lost_dict_technique, "technique")
            + _field_confidence(auction_dict_technique, "technique")
        ) / 2.0

    return material_score, technique_score
