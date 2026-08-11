"""
This module implements a loading function for technique and 
material hierarchies, which are used in similarity calculations.
"""

import csv
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MATERIAL_HIERARCHY: Dict[str, Optional[str]] = {}  # name -> parent
TECHNIQUE_HIERARCHY: Dict[str, Optional[str]] = {}  # name -> parent

MATERIAL_PATHS_TO_ROOT: Dict[str, List[str]] = {}  # name -> [path to root]
TECHNIQUE_PATHS_TO_ROOT: Dict[str, List[str]] = {}  # name -> [path to root]

ROOT_NAME_MATERIAL = "unbekannt"
ROOT_NAME_TECHNIQUE = "unbekannt"

IS_LOADED = False

# load CSV files and precompute all paths to root.
def load_hierarchies() -> None:

    global MATERIAL_HIERARCHY, TECHNIQUE_HIERARCHY
    global MATERIAL_PATHS_TO_ROOT, TECHNIQUE_PATHS_TO_ROOT
    global IS_LOADED
    
    if IS_LOADED:
        return
    
    csv_dir = Path(__file__).resolve().parents[3] / "shared" / "reference_data"
    
    # load material hierarchy
    material_csv = csv_dir / 'hierarchy_material.csv'
    if not material_csv.exists():
        raise FileNotFoundError(f"Material hierarchy CSV not found: {material_csv}")
    
    MATERIAL_HIERARCHY = _load_csv(material_csv, 'material_name', 'material_parent')
    MATERIAL_PATHS_TO_ROOT = _precompute_paths(MATERIAL_HIERARCHY, ROOT_NAME_MATERIAL)
    
    # load technique hierarchy
    technique_csv = csv_dir / 'hierarchy_technique.csv'
    if not technique_csv.exists():
        raise FileNotFoundError(f"Technique hierarchy CSV not found: {technique_csv}")
    
    TECHNIQUE_HIERARCHY = _load_csv(technique_csv, 'technique_name', 'technique_parent')
    TECHNIQUE_PATHS_TO_ROOT = _precompute_paths(TECHNIQUE_HIERARCHY, ROOT_NAME_TECHNIQUE)
    
    IS_LOADED = True
    logger.info(f"Loaded {len(MATERIAL_HIERARCHY)} materials and {len(TECHNIQUE_HIERARCHY)} techniques")


def _load_csv(
    csv_path: Path,
    name_col: str,
    parent_col: str
) -> Dict[str, Optional[str]]:
    hierarchy = {}
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter=';')
        for row in reader:
            name = row.get(name_col)
            parent = row.get(parent_col)
            
            if name:
                # store parent (None if empty)
                hierarchy[name] = parent if parent else None
    
    return hierarchy

# precompute path-to-root for all nodes in hierarchy.
# returns: {node_name: [path from node to root]}
def _precompute_paths(
    hierarchy: Dict[str, Optional[str]],
    root_name: str
) -> Dict[str, List[str]]:
    paths: Dict[str, List[str]] = {}
    
    # recursively compute path to root with cycle detection.
    def compute_path(name: str, visited: Optional[set] = None) -> List[str]:

        if visited is None:
            visited = set()
        
        # Already computed
        if name in paths:
            return paths[name]
        
        # Cycle detected
        if name in visited:
            return [name]
        
        visited.add(name)
        path = [name]
        current = name
        
        # Walk up to root
        while current and current in hierarchy:
            parent = hierarchy[current]
            
            if not parent or parent in visited or parent == root_name:
                # Reached root or cycle
                if parent:
                    path.append(parent)
                break
            
            path.append(parent)
            visited.add(parent)
            current = parent
        
        # Cache the result
        paths[name] = path
        return path
    
    # Precompute paths for ALL nodes (one time!)
    for name in hierarchy:
        if name not in paths:
            compute_path(name)
    
    return paths


def get_path_to_root(name: str, hierarchy_type: str = 'material') -> List[str]:
    """
    Get precomputed path to root for a node.
    - If `name` is falsy/None/empty or unknown, return the path containing the root node (e.g. ["unbekannt"]).
    - This ensures callers treat unknown values as pointing to the root.
    O(1) lookup - instant!
    """
    root_name = ROOT_NAME_MATERIAL if hierarchy_type == 'material' else ROOT_NAME_TECHNIQUE

    # Normalize missing/empty names -> return root
    if not name:
        return [root_name]

    # Direct lookup in the precomputed paths
    if hierarchy_type == 'material':
        path = MATERIAL_PATHS_TO_ROOT.get(name)
    else:
        path = TECHNIQUE_PATHS_TO_ROOT.get(name)

    if path:
        return path

    # Unknown name -> treat as root (unbekannt)
    return [root_name]


def get_root_name(hierarchy_type: str = 'material') -> str:
    return ROOT_NAME_MATERIAL if hierarchy_type == 'material' else ROOT_NAME_TECHNIQUE