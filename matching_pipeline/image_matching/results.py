"""Structured result types for the image-matching stage."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AcceptedImageMatch:
    auction_file_id: str
    auction_file_path: str
    lost_file_id: str
    lost_file_path: str
    confidence: float
    blocking_score: float | None = None
    keypoint_matches: dict[str, object] | None = None

    def as_csv_row(self) -> dict[str, object]:
        return {
            "auction_file_id": self.auction_file_id,
            "auction_file_path": self.auction_file_path,
            "lost_file_id": self.lost_file_id,
            "lost_file_path": self.lost_file_path,
            "confidence": self.confidence,
            "blocking_score": self.blocking_score,
        }


@dataclass(frozen=True)
class ImageMatchingRunResult:
    processed_auction_file_ids: list[str]
    accepted_matches: list[AcceptedImageMatch]
    pairs_processed: int
    failed_images: int
    failed_pairs: int
    lost_content_revision: int | None = None
    auction_content_versions: dict[str, int | None] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.processed_auction_file_ids)
