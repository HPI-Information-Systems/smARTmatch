from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup


def extract_html_details(html: str) -> dict[str, Any]:
    details: dict[str, Any] = {}

    try:
        soup = BeautifulSoup(html, "lxml")
        _extract_header_fields(soup, details)
        _extract_accordion_fields(soup, details)
        _extract_specialist_fields(soup, details)
    except Exception:
        return details

    return details


def _extract_header_fields(soup: BeautifulSoup, details: dict[str, Any]) -> None:
    pre_lot_desc = soup.find("div", class_="chr-lot-header__description")
    if pre_lot_desc:
        pre_lot_span = pre_lot_desc.find("span", class_="chr-body-s")
        if pre_lot_span:
            details["preLotText"] = pre_lot_span.get_text(strip=True)

    title_tag = soup.find("h1", class_="chr-lot-header__title")
    if not title_tag:
        title_tag = soup.find("h1", class_="chr-lot-header__title-text")
    if title_tag:
        details["title_txt"] = title_tag.get_text(" ", strip=True)

    artist_tag = soup.find("span", class_="chr-lot-header__artist-name")
    if artist_tag:
        artist_name = artist_tag.get_text(" ", strip=True)
        if artist_name:
            details["artistName"] = artist_name
            details["artist_name"] = artist_name

    description_span = soup.find(class_="chr-lot-section__accordion--text") or soup.find(
        class_="chr-lot-section__accordion--content"
    )
    if description_span:
        details["description"] = description_span.get_text("\n", strip=True)


def _extract_accordion_fields(soup: BeautifulSoup, details: dict[str, Any]) -> None:
    accordion_items = list(soup.find_all("chr-accordion-item"))
    accordion_items.extend(soup.find_all(class_="chr-accordion-item"))

    seen_items: set[int] = set()
    for item in accordion_items:
        item_id = id(item)
        if item_id in seen_items:
            continue
        seen_items.add(item_id)

        header_node = item.select_one('[slot="header"]') or item.find("div", slot="header")
        if not header_node:
            continue

        header_text = header_node.get_text(" ", strip=True).lower()
        if not header_text:
            continue

        content_node = (
            item.find(class_="chr-lot-section__accordion--content")
            or item.find(class_="chr-lot-section__accordion--text")
            or item.select_one('[slot="content"]')
        )
        if not content_node:
            continue

        content_text = content_node.get_text("\n", strip=True)
        if not content_text:
            continue

        if header_text == "details":
            details["details"] = content_text
        elif header_text == "provenance":
            details["provenance"] = content_text
        elif header_text == "literature":
            details["literature"] = content_text
        elif header_text in {"exhibited", "exhibition"}:
            details["exhibited"] = content_text
        elif header_text == "condition report":
            details["conditionReport"] = content_text


def _extract_specialist_fields(soup: BeautifulSoup, details: dict[str, Any]) -> None:
    specialist_container = soup.find(class_="chr-specialist-info")
    if not specialist_container:
        return

    specialist: dict[str, Any] = {}

    name_elem = specialist_container.find(class_="chr-specialist-info__name")
    if name_elem:
        specialist["name"] = name_elem.get_text(strip=True)

    category_elem = specialist_container.find(class_="chr-specialist-info__category")
    if category_elem:
        specialist["category"] = category_elem.get_text(strip=True)

    if specialist:
        details["chr-specialist"] = specialist
