"""Transform image-level matches into match_score DB write rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence
from uuid import UUID

from matching_pipeline.image_matching.results import AcceptedImageMatch


@dataclass(frozen=True)
class ImageMatchScoreWrite:
    lost_artwork_id: UUID
    auction_artwork_id: UUID
    image_matching_confidence: float
    image_final_score: float
    image_blocking_similarity: float | None
    best_image_file_id: int
    image_visualization: dict[str, object]


def coerce_image_file_ids(values: Sequence[str], role: str) -> list[int]:
    """Coerce image_file_id strings from artifacts to positive integer DB ids."""
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        image_file_id = _coerce_image_file_id(value, role)
        if image_file_id not in seen:
            seen.add(image_file_id)
            result.append(image_file_id)
    return result


def prepare_match_score_image_writes(
    accepted_matches: Sequence[AcceptedImageMatch],
    *,
    auction_links: Mapping[int, Sequence[UUID]],
    lost_links: Mapping[int, Sequence[UUID]],
    matching_program_id: UUID,
) -> list[ImageMatchScoreWrite]:
    """Aggregate accepted image matches into one best row per lost/auction pair."""
    grouped: dict[tuple[UUID, UUID], dict[str, object]] = {}
    for match in accepted_matches:
        auction_file_id = _coerce_image_file_id(match.auction_file_id, "auction_file_id")
        lost_file_id = _coerce_image_file_id(match.lost_file_id, "lost_file_id")
        confidence = _coerce_score(match.confidence, "image matching confidence")
        blocking_score = _coerce_blocking_score(match.blocking_score)
        final_score = _image_final_score(confidence, blocking_score)
        source = _source_match(
            match,
            auction_file_id,
            lost_file_id,
            confidence,
            blocking_score,
            final_score,
        )
        for auction_artwork_id in _required_links(auction_links, auction_file_id, "auction"):
            for lost_artwork_id in _required_links(lost_links, lost_file_id, "lost"):
                key = (lost_artwork_id, auction_artwork_id)
                current = grouped.get(key)
                if current is None or _is_better(
                    final_score,
                    confidence,
                    blocking_score,
                    current,
                ):
                    grouped[key] = {
                        "confidence": confidence,
                        "final_score": final_score,
                        "blocking_score": blocking_score,
                        "best_image_file_id": auction_file_id,
                        "source": source,
                    }

    return [
        _make_write(lost_id, auction_id, matching_program_id, item)
        for (lost_id, auction_id), item in sorted(
            grouped.items(), key=lambda row: (str(row[0][0]), str(row[0][1]))
        )
    ]


def _make_write(
    lost_artwork_id: UUID,
    auction_artwork_id: UUID,
    matching_program_id: UUID,
    item: Mapping[str, object],
) -> ImageMatchScoreWrite:
    confidence = float(item["confidence"])
    final_score = float(item["final_score"])
    blocking_score = item["blocking_score"]
    return ImageMatchScoreWrite(
        lost_artwork_id=lost_artwork_id,
        auction_artwork_id=auction_artwork_id,
        image_matching_confidence=confidence,
        image_final_score=final_score,
        image_blocking_similarity=(
            None if blocking_score is None else float(blocking_score)
        ),
        best_image_file_id=int(item["best_image_file_id"]),
        image_visualization={
            "image_matching": {
                "matching_program_id": str(matching_program_id),
                "best_match": item["source"],
            }
        },
    )


def _is_better(
    final_score: float,
    confidence: float,
    blocking_score: float | None,
    current: Mapping[str, object],
) -> bool:
    current_final = float(current["final_score"])
    if final_score != current_final:
        return final_score > current_final
    current_confidence = float(current["confidence"])
    if confidence != current_confidence:
        return confidence > current_confidence
    current_blocking = current.get("blocking_score")
    if current_blocking is None:
        return blocking_score is not None
    if blocking_score is None:
        return False
    return blocking_score > float(current_blocking)


def _source_match(
    match: AcceptedImageMatch,
    auction_file_id: int,
    lost_file_id: int,
    confidence: float,
    blocking_score: float | None,
    final_score: float,
) -> dict[str, object]:
    result: dict[str, object] = {
        "auction_image_file_id": auction_file_id,
        "auction_file_path": match.auction_file_path,
        "lost_image_file_id": lost_file_id,
        "lost_file_path": match.lost_file_path,
        "image_matching_confidence": confidence,
        "image_blocking_similarity": blocking_score,
        "image_final_score": final_score,
    }
    if match.keypoint_matches is not None:
        result["keypoint_matches"] = match.keypoint_matches
    return result


def _required_links(
    links: Mapping[int, Sequence[UUID]], image_file_id: int, role: str
) -> Sequence[UUID]:
    values = links.get(image_file_id, ())
    if not values:
        raise ValueError(f"No {role} artwork link found for image_file_id={image_file_id}")
    return values


def _image_final_score(confidence: float, blocking_score: float | None) -> float:
    if blocking_score is None:
        return confidence
    return confidence * blocking_score


def _coerce_image_file_id(value: object, field_name: str) -> int:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Missing {field_name}")
    try:
        image_file_id = int(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer image_file_id: {text!r}") from exc
    if image_file_id <= 0:
        raise ValueError(f"{field_name} must be positive: {image_file_id}")
    return image_file_id


def _coerce_score(value: object, field_name: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if score < 0 or score > 1:
        raise ValueError(f"{field_name} must be in [0, 1]: {score}")
    return score


def _coerce_blocking_score(value: object) -> float | None:
    if value is None:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid image blocking similarity: {value!r}") from exc
    if score < -1 or score > 1:
        raise ValueError(f"Image blocking similarity must be in [-1, 1]: {score}")
    return score
