from __future__ import annotations

import json
import random
import time
from typing import Optional

import requests

from ..utils.request_handler import generate_headers, request_html
from .constants import GRAPHQL_ENDPOINT, GRAPHQL_QUERY, LOT_CARDS_QUERY, SOTHEBYS_ORIGIN
from .discovery import (
    calendar_page_url,
    extract_auction_id,
    extract_buy_links,
)
from .models import AuctionContext


class SothebysClient:
    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        min_wait: float = 0.25,
        max_wait: float = 0.75,
        log=print,
    ):
        self.session = session or requests.Session()
        self.min_wait = min_wait
        self.max_wait = max_wait
        # ``log`` is the bound ``Scraper.log`` of the owning scraper.
        self._log = log

    def _sleep(self) -> None:
        time.sleep(random.uniform(self.min_wait, self.max_wait))

    @staticmethod
    def calendar_page_url(base: str, page_number: int) -> str:
        return calendar_page_url(base, page_number)

    def fetch_calendar_html(self, url: str) -> str:
        self._sleep()
        headers = generate_headers()
        try:
            response = self.session.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            text = response.text
        except Exception:
            text = request_html(url, min_wait=self.min_wait, max_wait=self.max_wait, log=self._log) or ""

        trimmed = text.lstrip()
        if trimmed.startswith("{"):
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    for key in ("html", "content", "markup"):
                        value = data.get(key)
                        if isinstance(value, str) and value.strip():
                            return value
            except Exception:
                pass
        return text

    @staticmethod
    def extract_buy_links(html: str, *, base_url: str) -> list[str]:
        return extract_buy_links(html, base_url=base_url)

    @staticmethod
    def extract_auction_id(html: str) -> str:
        return extract_auction_id(html)

    def fetch_auction_context(self, auction_url: str) -> Optional[AuctionContext]:
        self._sleep()
        headers = generate_headers()
        try:
            response = self.session.get(auction_url, headers=headers, timeout=30)
            response.raise_for_status()
            html = response.text
        except Exception as exc:
            self._log(f"[auction] [fail] fetch {auction_url}: {exc}")
            return None

        try:
            auction_id = self.extract_auction_id(html)
        except Exception as exc:
            self._log(f"[auction] [fail] parse {auction_url}: {exc}")
            return None

        return AuctionContext(auction_url=auction_url, auction_id=auction_id)

    def fetch_auction_lot_ids(
        self,
        auction_id: str,
        *,
        language: str = "ENGLISH",
        page_size: int = 48,
    ) -> list[str]:
        """Page through ``lotCardsConnection`` and return every lotId in order."""

        headers = self._graphql_headers()
        lot_ids: list[str] = []
        seen: set[str] = set()
        offset = 0

        while True:
            self._sleep()
            payload = {
                "operationName": "LotCardsFilterByPaginated",
                "variables": {
                    "id": auction_id,
                    "filter": "ALL",
                    "language": language,
                    "limit": page_size,
                    "offset": offset,
                },
                "query": LOT_CARDS_QUERY,
            }

            response = self.session.post(
                GRAPHQL_ENDPOINT,
                headers=headers,
                data=json.dumps(payload),
                timeout=30,
            )
            response.raise_for_status()
            body = response.json() if response.content else {}

            if isinstance(body, dict) and body.get("errors"):
                first = body["errors"][0] if isinstance(body["errors"], list) and body["errors"] else {}
                message = first.get("message") if isinstance(first, dict) else None
                raise RuntimeError(f"lotCardsConnection error for {auction_id}: {message or 'unknown error'}")

            connection = (
                body.get("data", {})
                .get("auction", {})
                .get("lotCards", {})
                if isinstance(body, dict)
                else {}
            )
            lots = connection.get("lots") or []

            for lot in lots:
                lot_id = lot.get("lotId") if isinstance(lot, dict) else None
                if isinstance(lot_id, str) and lot_id and lot_id not in seen:
                    seen.add(lot_id)
                    lot_ids.append(lot_id)

            if not connection.get("hasNextPage") or not lots:
                break

            offset += page_size

        return lot_ids

    def fetch_lot_response(self, *, lot_id: str, country: str, language: str) -> Optional[dict]:
        self._sleep()

        headers = self._graphql_headers()

        payload = {
            "operationName": "LotQuery",
            "variables": {
                "id": lot_id,
                "countryOfOrigin": country,
                "language": language,
            },
            "query": GRAPHQL_QUERY,
        }

        try:
            response = self.session.post(
                GRAPHQL_ENDPOINT,
                headers=headers,
                data=json.dumps(payload),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            self._log(f"[graphql] [fail] lot {lot_id}: {exc}")
            return None

        if isinstance(data, dict) and data.get("errors"):
            first = data.get("errors")[0] if isinstance(data.get("errors"), list) and data.get("errors") else None
            message = first.get("message") if isinstance(first, dict) else None
            self._log(
                f"[graphql] [fail] lot {lot_id} returned errors: "
                f"{message or 'unknown error'}"
            )
            return None

        return data

    @staticmethod
    def _graphql_headers() -> dict[str, str]:
        return {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": SOTHEBYS_ORIGIN,
            "referer": SOTHEBYS_ORIGIN + "/",
            "apollographql-client-name": "Bidclient",
            "user-agent": generate_headers().get("User-Agent", "Mozilla/5.0"),
        }
