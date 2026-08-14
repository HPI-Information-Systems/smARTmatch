from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from ..utils.user_agents import VERIFIED_USER_AGENTS, choose_user_agent


DEFAULT_USER_AGENTS = VERIFIED_USER_AGENTS


@dataclass(frozen=True)
class SearchClientConfig:
    datasource_id: str
    page_id: str
    geocountrycode: str = "DE"
    page_size: int = 50
    filter_ids: str = (
        "|CoaCategoryValues{Paintings}|"
        "CoaCategoryValues{Prints+%26+Multiples}|"
        "CoaCategoryValues{Drawings+%26+Watercolors}|"
        "CoaCategoryValues{Photographs}|"
        "CoaCategoryValues{All+other+categories+of+objects}|"
    )


@dataclass(frozen=True)
class ChristiesAPIConfig:
    base_url: str = "https://apim.christies.com"
    keyword: str = ""
    search_client: SearchClientConfig = SearchClientConfig(
        datasource_id="87eea736-0dca-475d-a84e-51bb79342e64",
        page_id="6be0aad6-a159-4f44-a4ff-eb544a12d9b8",
    )
    rotate_after_requests: int = 200
    base_delay: float = 0.05
    user_agents: tuple[str, ...] = DEFAULT_USER_AGENTS


class ChristiesAPI:
    def __init__(self, config: ChristiesAPIConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.request_count = 0

    def _get_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.christies.v1+json",
            "User-Agent": choose_user_agent(self.config.user_agents),
            "Referer": "https://www.christies.com/en/search",
            "Origin": "https://www.christies.com",
        }

    def _apply_delay(self) -> None:
        time.sleep(self.config.base_delay)
        self.request_count += 1

        if self.request_count % self.config.rotate_after_requests == 0:
            self.session.close()
            self.session = requests.Session()

    def get_search_client_results(
        self, *, from_offset: int = 0
    ) -> Optional[dict[str, Any]]:
        url = f"{self.config.base_url}/search-client"
        params = {
            "sortby": "relevance",
            "language": "en",
            "geocountrycode": self.config.search_client.geocountrycode,
            "use_full_field_set": "false",
            "use_lots_availability": "true",
            "show_on_loan": "true",
            "datasourceId": self.config.search_client.datasource_id,
            "pageId": self.config.search_client.page_id,
            "filterids": self.config.search_client.filter_ids,
            "from": str(from_offset),
            "isInitialRequest": ("false" if from_offset > 0 else "true"),
        }

        self._apply_delay()

        try:
            response = self.session.get(
                url, params=params, headers=self._get_headers(), timeout=30
            )
            if response.status_code != 200:
                return None
            data = response.json()
            return {
                "lots": data.get("lots", []),
                "total_pages": data.get("total_pages", 0),
                "facets": data.get("facets", {}),
            }
        except Exception:
            return None
