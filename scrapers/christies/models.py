from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class ChristiesLotFields:
    title: str
    description: Optional[str]
    provenance: Optional[str]
    material: Optional[str]
    technique: Optional[str]
    dating: Optional[str]
    condition: Optional[str]
    signature: Optional[str]
    literature: Optional[str]
    auction_date: Optional[date]
    width: Optional[float]
    height: Optional[float]
    payload_artist: Optional[str]
