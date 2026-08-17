from __future__ import annotations

import random
import re
import time
from typing import Iterable, Optional

from bs4 import BeautifulSoup
from sqlalchemy import text

from ..db_interface import Database
from ..utils.auction_helpers import fit_varchar, json_dumps
from ..utils.auction_scraper import AuctionPlatformScraper
from .api import ChristiesAPI, ChristiesAPIConfig
from .entities import resolve_artist_id, resolve_expert_id
from .html import ChristiesHTMLScraper
from .lot_parser import ChristiesLotParser
from .parser import (
    build_auction_details,
    extract_artist_name,
    extract_image_urls,
    normalize_text,
    stringify_dict,
)
from .transform import resolve_lot_fields, should_log_resolution_debug


class ChristiesScraper(AuctionPlatformScraper):
    """Scrape Christie's lots into Postgres."""

    def __init__(
        self,
        *,
        db: Optional[Database] = None,
        max_pages: Optional[int] = None,
        min_wait: float = 0.25,
        max_wait: float = 0.75,
        download_images: bool = True,
        images_dir: Optional[str] = None,
        max_rows: Optional[int] = None,
        commit_every: int = 50,
        purge: bool = False,
    ) -> None:
        super().__init__(
            db=db,
            platform_name="Christie's",
            min_wait=min_wait,
            max_wait=max_wait,
            purge=purge,
            commit_every=commit_every,
            download_images=download_images,
            images_dir=images_dir,
            module_file=__file__,
        )
        self.max_pages = max_pages
        self.max_rows = max_rows

        self.api = ChristiesAPI(ChristiesAPIConfig())
        self.html = ChristiesHTMLScraper(log=self.log)
        self.lot_parser = ChristiesLotParser()

        self._api_cache: dict[str, dict[str, object]] = {}

    def get_urls(self, skip: int = 0) -> Iterable[str]:
        page_size = self.api.config.search_client.page_size
        from_offset = 0
        total_pages = None
        seen: set[str] = set()
        urls: list[str] = []

        while True:
            if self.max_pages is not None and total_pages is not None:
                if from_offset // page_size >= self.max_pages:
                    break

            result = self.api.get_search_client_results(from_offset=from_offset)
            if result is None:
                break

            if total_pages is None:
                total_pages = result.get("total_pages", 0)

            lots = result.get("lots", [])
            if not lots:
                break

            for lot in lots:
                lot_id = self.lot_parser.extract_lot_id(lot)
                if not lot_id or lot_id in seen:
                    continue

                seen.add(lot_id)
                self._api_cache[lot_id] = {"lot": lot}
                urls.append(lot_id)

                if self.max_rows is not None and len(urls) >= self.max_rows + skip:
                    break

            if self.max_rows is not None and len(urls) >= self.max_rows + skip:
                break

            from_offset += page_size
            if self.min_wait > 0 or self.max_wait > 0:
                time.sleep(random.uniform(self.min_wait, self.max_wait))

            if total_pages is not None and from_offset >= total_pages * page_size:
                break

        if skip:
            urls = urls[skip:]

        self.log(f"[load] {len(urls)} lot IDs from search pages")
        return urls

    def scrape_url(self, url: str):
        lot_id = url
        lot_url = self.lot_parser.lot_url_for_id(lot_id)

        lot_data, sale_data, specialist = self._load_lot_sources(lot_id)
        if not lot_data:
            return None

        page_title, page_artist = self.lot_parser.extract_title_artist_from_webpage(
            lot_url
        )
        page_title, page_artist, suppress_payload_artist = (
            self._normalize_page_title_artist(
                lot_data=lot_data,
                page_title=page_title,
                page_artist=page_artist,
            )
        )
        fields = resolve_lot_fields(
            lot_id=lot_id,
            lot_data=lot_data,
            sale_data=sale_data,
            page_title=page_title,
        )

        platform = self.get_platform()
        expert_id = resolve_expert_id(self.db, specialist)

        explicit_artist = extract_artist_name(lot_data)
        artist_name = self._resolve_artist_name(
            explicit_artist=explicit_artist,
            page_artist=page_artist,
            payload_artist=fields.payload_artist,
            suppress_payload_artist=suppress_payload_artist,
        )

        artist_id = resolve_artist_id(self.db, artist_name)
        if should_log_resolution_debug(title=fields.title, artist_name=artist_name):
            self.log(
                "[debug] "
                f"lot_id={lot_id} "
                f"web_title={page_title or '—'} "
                f"web_artist={page_artist or '—'} "
                f"resolved_title={fields.title or '—'} "
                f"resolved_artist={artist_name or '—'}"
            )

        artwork_id = self.resolve_storage_artwork_id(
            lot_id=lot_id,
            lot_url=lot_url,
            platform_id=platform.auction_platform_id,
        )
        image_urls = extract_image_urls(lot_data.get("lot_assets"))
        image_paths = self.download_lot_images(
            image_urls,
            lot_id=lot_id,
            lot_url=lot_url,
            artwork_id=artwork_id,
        )

        auction_details = build_auction_details(lot_data, sale_data)
        raw_data = {
            "lot": lot_data,
            "sale": sale_data,
            "specialist": specialist,
        }

        artist_name_for_row = fit_varchar(artist_name)
        artwork = self.db.upsert_auction_artwork(
            auction_artwork_id=artwork_id,
            lot_id=lot_id,
            lot_url=lot_url,
            title=fields.title,
            artist_full_name=artist_name_for_row,
            artist_raw_data=(
                json_dumps({"source": "christies", "name": artist_name_for_row})
                if artist_name_for_row
                else None
            ),
            description=fields.description,
            provenance=fields.provenance,
            material=fields.material,
            technique=fields.technique,
            dating=fields.dating,
            condition=fields.condition,
            signature=fields.signature,
            literature=fields.literature,
            width=fields.width,
            height=fields.height,
            auction_details=json_dumps(auction_details),
            auction_date=fields.auction_date,
            auction_platform_id=platform.auction_platform_id,
            expert_id=expert_id,
            artist_id=artist_id,
            raw_data=stringify_dict(raw_data),
        )
        self.set_lot_images(
            artwork_id=artwork.auction_artwork_id,
            image_paths=image_paths,
        )

        if artist_name_for_row is None:
            self._clear_artist_columns(lot_id=lot_id)

        if self.min_wait > 0 or self.max_wait > 0:
            time.sleep(random.uniform(self.min_wait, self.max_wait))

        return None

    def _load_lot_sources(
        self,
        lot_id: str,
    ) -> tuple[dict[str, object], dict[str, object], Optional[dict[str, object]]]:
        api_data = self._api_cache.get(lot_id, {}).get("lot")

        if lot_id.startswith("SN") or lot_id.startswith("MS"):
            html_data = self.html.fetch_private_item_html(lot_id)
        else:
            html_data, _ = self.html.fetch_lot_html(lot_id, verbose=False)

        return self.lot_parser.merge_data(api_data, html_data)

    @classmethod
    def _normalize_page_title_artist(
        cls,
        *,
        lot_data: dict[str, object],
        page_title: Optional[str],
        page_artist: Optional[str],
    ) -> tuple[Optional[str], Optional[str], bool]:
        title_primary = normalize_text(lot_data.get("title_primary_txt"))
        title_secondary = normalize_text(lot_data.get("title_secondary_txt"))
        page_title_clean = normalize_text(page_title)
        page_artist_clean = normalize_text(page_artist)

        looks_swapped = bool(
            title_primary
            and title_secondary
            and page_title_clean == title_secondary
            and page_artist_clean == title_primary
        )

        resolved_title = page_title
        resolved_artist = page_artist

        if looks_swapped and cls._looks_like_object_title(title_primary):
            resolved_title = title_primary

        if looks_swapped:
            resolved_artist = None

        suppress_payload_artist = looks_swapped
        return resolved_title, resolved_artist, suppress_payload_artist

    @staticmethod
    def _looks_like_object_title(value: Optional[str]) -> bool:
        text = normalize_text(value)
        if not text:
            return False

        return bool(
            re.match(
                r"^(?:(?:A|AN|THE)\s+(?:[A-ZÀ-Ý][A-Za-zÀ-ÿ]{2,}|\d)|"
                r"TWO\b|THREE\b|FOUR\b|FIVE\b|SIX\b|SEVEN\b|EIGHT\b|NINE\b|TEN\b|"
                r"PAIR\s+OF\b|SET\s+OF\b|GROUP\s+OF\b)",
                text,
                flags=re.IGNORECASE,
            )
        )

    @classmethod
    def _normalize_artist_candidate(cls, value: Optional[str]) -> Optional[str]:
        text = normalize_text(value)
        if not text:
            return None
        if cls._looks_like_object_title(text):
            return None
        return text

    @classmethod
    def _resolve_artist_name(
        cls,
        *,
        explicit_artist: Optional[str],
        page_artist: Optional[str],
        payload_artist: Optional[str],
        suppress_payload_artist: bool,
    ) -> Optional[str]:
        explicit_candidate = cls._normalize_artist_candidate(explicit_artist)
        if explicit_candidate:
            return explicit_candidate

        page_candidate = cls._normalize_artist_candidate(page_artist)
        if page_candidate:
            return page_candidate

        if suppress_payload_artist:
            return None

        return cls._normalize_artist_candidate(payload_artist)

    def _clear_artist_columns(self, *, lot_id: str) -> None:
        actual_columns = self.db._get_table_columns("auction_artwork")
        nullable_artist_columns = [
            column
            for column in ("artist_id", "artist_full_name", "artist_raw_data")
            if column in actual_columns
        ]
        if not nullable_artist_columns:
            return

        set_clause = ", ".join(f"{column} = null" for column in nullable_artist_columns)
        session = self.db._get_session()
        session.execute(
            text(f"update auction_artwork set {set_clause} where lot_id = :lot_id"),
            {"lot_id": lot_id},
        )

    # Compatibility wrappers for existing tests/helpers.
    def _extract_title_artist_from_soup(
        self, soup: BeautifulSoup
    ) -> tuple[Optional[str], Optional[str]]:
        return self.lot_parser.extract_title_artist_from_soup(soup)

    def _extract_title_artist_from_webpage(
        self, lot_url: str
    ) -> tuple[Optional[str], Optional[str]]:
        return self.lot_parser.extract_title_artist_from_webpage(lot_url)
