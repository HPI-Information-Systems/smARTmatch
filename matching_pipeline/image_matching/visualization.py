"""Build frontend-renderable LightGlue keypoint-match visualization payloads."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def build_keypoint_match_visualization(
    auction_features: Mapping[str, Any],
    lost_features: Mapping[str, Any],
    lightglue_matches: Mapping[str, Any],
) -> dict[str, object]:
    """Return pixel-coordinate keypoint matches for an auction/lost image pair."""
    auction_keypoints = _keypoint_rows(auction_features.get("keypoints"), "auction")
    lost_keypoints = _keypoint_rows(lost_features.get("keypoints"), "lost")
    pairs = _match_pairs(lightglue_matches)
    scores = _match_scores(lightglue_matches, pairs)

    rows: list[dict[str, object]] = []
    for offset, (auction_index, lost_index) in enumerate(pairs):
        if auction_index >= len(auction_keypoints) or lost_index >= len(lost_keypoints):
            raise ValueError(
                "LightGlue match index out of range: "
                f"auction={auction_index}/{len(auction_keypoints)} "
                f"lost={lost_index}/{len(lost_keypoints)}"
            )
        row: dict[str, object] = {
            "auction_keypoint_index": auction_index,
            "lost_keypoint_index": lost_index,
            "auction": _point_payload(auction_keypoints[auction_index]),
            "lost": _point_payload(lost_keypoints[lost_index]),
        }
        if offset < len(scores):
            row["score"] = float(scores[offset])
        rows.append(row)

    return {
        "coordinate_space": "image_pixels",
        "match_count": len(rows),
        "matches": rows,
    }


def _match_pairs(matches: Mapping[str, Any]) -> list[tuple[int, int]]:
    if "matches" in matches:
        return _pairs_from_rows(_to_rows(matches["matches"], "matches"))
    if "matches0" in matches:
        return [
            (index, lost_index)
            for index, lost_index in enumerate(_to_int_vector(matches["matches0"], "matches0"))
            if lost_index >= 0
        ]
    raise ValueError("LightGlue output is missing matches/matches0")


def _match_scores(matches: Mapping[str, Any], pairs: Sequence[tuple[int, int]]) -> list[float]:
    if "scores" in matches:
        return _to_float_vector(matches["scores"], "scores")
    if "matching_scores0" in matches:
        scores0 = _to_float_vector(matches["matching_scores0"], "matching_scores0")
        return [scores0[auction_index] for auction_index, _ in pairs if auction_index < len(scores0)]
    return []


def _pairs_from_rows(rows: Sequence[Sequence[Any]]) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    for row in rows:
        if len(row) < 2:
            raise ValueError(f"LightGlue match row must have two indices: {row!r}")
        auction_index = int(row[0])
        lost_index = int(row[1])
        if auction_index >= 0 and lost_index >= 0:
            pairs.append((auction_index, lost_index))
    return pairs


def _keypoint_rows(value: Any, role: str) -> list[tuple[float, float]]:
    rows = _to_rows(value, f"{role} keypoints")
    result: list[tuple[float, float]] = []
    for row in rows:
        if len(row) < 2:
            raise ValueError(f"{role} keypoint must have x/y coordinates: {row!r}")
        result.append((float(row[0]), float(row[1])))
    return result


def _point_payload(point: tuple[float, float]) -> dict[str, float]:
    return {"x": point[0], "y": point[1]}


def _to_rows(value: Any, field_name: str) -> list[Sequence[Any]]:
    rows = _to_python(value)
    if rows is None:
        raise ValueError(f"Missing {field_name}")
    rows = _unwrap_single_batch_rows(rows)
    if not isinstance(rows, list):
        raise ValueError(f"{field_name} must be a row list")
    return rows


def _to_int_vector(value: Any, field_name: str) -> list[int]:
    return [int(item) for item in _to_vector(value, field_name)]


def _to_float_vector(value: Any, field_name: str) -> list[float]:
    return [float(item) for item in _to_vector(value, field_name)]


def _to_vector(value: Any, field_name: str) -> list[Any]:
    vector = _to_python(value)
    if vector is None:
        raise ValueError(f"Missing {field_name}")
    vector = _unwrap_single_batch_vector(vector)
    if not isinstance(vector, list):
        raise ValueError(f"{field_name} must be a vector")
    return vector


def _unwrap_single_batch_rows(value: Any) -> Any:
    if (
        isinstance(value, list)
        and len(value) == 1
        and isinstance(value[0], list)
        and value[0]
        and isinstance(value[0][0], list)
    ):
        return value[0]
    return value


def _unwrap_single_batch_vector(value: Any) -> Any:
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
        return value[0]
    return value


def _to_python(value: Any) -> Any:
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "tolist"):
        return value.tolist()
    return value
