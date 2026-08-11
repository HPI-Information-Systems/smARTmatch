from __future__ import annotations

from .detail import DorotheumDetailMixin
from .listing import DorotheumListingMixin


class DorotheumLotParser(DorotheumListingMixin, DorotheumDetailMixin):
    """Parser composed from focused listing/detail mixins."""


__all__ = ["DorotheumLotParser"]
