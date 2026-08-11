from __future__ import annotations

from typing import Optional

from bs4 import BeautifulSoup, Tag

from .normalization import DorotheumNormalizationMixin


class DorotheumDetailMetaMixin(DorotheumNormalizationMixin):
    def _extract_status_and_tracking(self, soup: BeautifulSoup) -> tuple[dict[str, object], dict[str, object]]:
        lot_status = soup.select_one("div.lot-status")
        attrs = dict(lot_status.attrs) if isinstance(lot_status, Tag) else {}
        tracking = self._load_json(attrs.get("data-ga4-tracking-data"))
        return attrs, tracking if isinstance(tracking, dict) else {}

    def _extract_expert(self, soup: BeautifulSoup) -> tuple[Optional[str], Optional[str], Optional[str]]:
        for item in self._extract_schema_graph(soup):
            if item.get("@type") != "Person":
                continue
            return (
                self._clean_text(item.get("name")),
                self._clean_text(item.get("telephone")),
                self._clean_text(item.get("email")),
            )

        expert_container = soup.select_one("#experten-info-content")
        if not isinstance(expert_container, Tag):
            expert_container = soup.select_one("#experten-info-content--mobile")
        if not isinstance(expert_container, Tag):
            return None, None, None

        return (
            self._extract_expert_name(expert_container),
            self._extract_expert_link_text(expert_container, 'a[href^="tel:"]'),
            self._extract_expert_link_text(expert_container, 'a[href^="mailto:"]'),
        )

    def _extract_expert_name(self, expert_container: Tag) -> Optional[str]:
        name_col = expert_container.select_one(".col.col-8")
        if not isinstance(name_col, Tag):
            return None

        for part in name_col.stripped_strings:
            candidate = self._clean_text(part)
            if not candidate:
                continue
            if candidate.startswith("+") or "@" in candidate:
                continue
            return candidate
        return None

    def _extract_expert_link_text(self, expert_container: Tag, selector: str) -> Optional[str]:
        node = expert_container.select_one(selector)
        if not isinstance(node, Tag):
            return None
        return self._clean_text(node.get_text(" ", strip=True))

    def _extract_schema_graph(self, soup: BeautifulSoup) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            if not isinstance(script, Tag):
                continue

            payload = self._load_json(script.get_text())
            if not isinstance(payload, dict):
                continue

            graph = payload.get("@graph")
            if isinstance(graph, list):
                out.extend(item for item in graph if isinstance(item, dict))
            elif payload.get("@type"):
                out.append(payload)
        return out

    def _extract_email_share_data(self, soup: BeautifulSoup) -> dict[str, object]:
        node = soup.select_one("#email-share-form")
        if not isinstance(node, Tag):
            return {}

        out: dict[str, object] = {}
        for key in (
            "data-uid",
            "data-nr",
            "data-titel",
            "data-datum",
            "data-ort",
            "data-schaustellungdatum",
            "data-taxcode",
        ):
            value = self._clean_text(node.get(key))
            if value:
                out[key] = value
        return out
