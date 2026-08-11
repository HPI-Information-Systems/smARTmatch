""""
This module implements a similarity function for technique and material attributes.

Similarity in a rooted hierarchy tree using edge lengths is calculated as:
similarity = common_path_length / total_unique_path_length

 - Paths are counted from node to root.
 - Length means number of edges, not nodes.
- Shared part is the common suffix of both root paths.
- If either side is the root/unknown value, return None.
"""

from __future__ import annotations
from typing import Any
from matching_pipeline.metadata_matching.similarity_functions.technique_and_material.load_hierarchy import (
    get_path_to_root,
    get_root_name,
)

# normalize dict array values passed from DB/tests into a list of names.
def _extract_names(value: Any) -> list[str]:
 
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if item is not None and str(item).strip()]

    text = str(value).strip()
    if not text:
        return []

    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]

    if "," in text:
        return [part.strip().strip('"') for part in text.split(",") if part.strip().strip('"')]

    return [text.strip('"')]


def _get_parent_distance(
    name1: str, name2: str, hierarchy_type: str = "material"
) -> float | None:

    if not name1 or not name2:
        return None

    root_name = get_root_name(hierarchy_type)

    if name1.lower() == root_name.lower() or name2.lower() == root_name.lower():
        return None

    if name1 == name2:
        return 1.0

    path1 = get_path_to_root(name1, hierarchy_type)
    path2 = get_path_to_root(name2, hierarchy_type)

    rev1 = list(reversed(path1))
    rev2 = list(reversed(path2))

    common_nodes = 0
    for node1, node2 in zip(rev1, rev2):
        if node1 == node2:
            common_nodes += 1
        else:
            break

    common_edges = max(0, common_nodes - 1)
    edges1 = max(0, len(path1) - 1)
    edges2 = max(0, len(path2) - 1)
    total_unique_edges = edges1 + edges2 - common_edges

    if total_unique_edges <= 0:
        return 1.0

    return common_edges / total_unique_edges


def _calculate_dict_similarity(
    lost_dict_names: list[str],
    auc_dict_names: list[str],
    hierarchy_type: str,
) -> float | None:

    lost_names = _extract_names(lost_dict_names)
    auc_names = _extract_names(auc_dict_names)

    if not lost_names or not auc_names:
        return None

    best_similarity: float | None = None

    for lost_name in lost_names:
        for auc_name in auc_names:
            sim = _get_parent_distance(lost_name, auc_name, hierarchy_type)

            if sim is None:
                continue

            if best_similarity is None or sim > best_similarity:
                best_similarity = sim

    return best_similarity


def similarity_function(
    lost_dict_material: list[str],
    lost_dict_technique: list[str],
    auc_dict_material: list[str],
    auc_dict_technique: list[str],
) -> tuple[float | None, float | None]:

    material_similarity = _calculate_dict_similarity(
        _extract_names(lost_dict_material),
        _extract_names(auc_dict_material),
        hierarchy_type="material",
    )
    technique_similarity = _calculate_dict_similarity(
        _extract_names(lost_dict_technique),
        _extract_names(auc_dict_technique),
        hierarchy_type="technique",
    )

    return material_similarity, technique_similarity
