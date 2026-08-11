from __future__ import annotations

from bs4 import BeautifulSoup


class DrouotDescriptionMixin:
    def _extract_description_from_dom(self, soup: BeautifulSoup) -> str:
        preferred_nodes = [
            node
            for node in soup.find_all(["p", "div"])
            if "whitespace-pre-line" in (node.get("class") or [])
        ]
        for node in preferred_nodes:
            text = node.get_text("\n", strip=True)
            if not text or self._is_boilerplate_description(text):
                continue
            return text

        for paragraph in soup.find_all("p"):
            text = paragraph.get_text("\n", strip=True)
            if not text or self._is_boilerplate_description(text):
                continue
            if len(text) > 200:
                return text
        return ""

    @staticmethod
    def _is_boilerplate_description(text: str) -> bool:
        lower = text.casefold()
        return any(
            phrase in lower
            for phrase in (
                "text übersetzt von deepl",
                "text translated by deepl",
                "prix de réserve",
                "reserve price",
                "conditions générales de vente",
                "general conditions of sale",
            )
        )
