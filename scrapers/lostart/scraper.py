from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import UUID

import requests
from bs4 import BeautifulSoup

from shared.image_storage_lock import image_storage_lock

from ..db_interface import Database, LostArtwork
from ..utils.image_storage import resolve_images_dir, safe_image_prefix
from ..utils.scraper import Scraper
from . import detail_scraper as lostart_detail_scraper
from . import image_scraper as lostart_image_scraper


@dataclass(frozen=True)
class LostArtRecord:
    lost_art_id: str
    url: str
    index_row: dict[str, str]


class LostArtScraper(Scraper):
    """Scrape Lost Art records into Postgres."""

    _env_loaded = False

    def __init__(
        self,
        *,
        db: Optional[Database] = None,
        max_pages: Optional[int] = None,
        page_delay: float = 0.0,
        start_offset: int = 0,
        details_sleep: float = 0.5,
        download_images: bool = True,
        images_dir: Optional[str] = None,
        max_rows: Optional[int] = None,
        commit_every: int = 50,
        purge: bool = False,
        anubis_cookie_verification: Optional[str] = None,
        anubis_auth: Optional[str] = None,
        cookie_header: Optional[str] = None,
        search_url: Optional[str] = None,
    ) -> None:
        super().__init__(db=db, log_prefix="los")
        self._load_lostart_env_file()

        self.max_pages = max_pages
        self.page_delay = page_delay
        self.start_offset = max(0, int(start_offset))
        self.details_sleep = details_sleep
        self.download_images_enabled = download_images
        self.images_dir = resolve_images_dir(
            module_file=__file__, images_dir=images_dir
        )
        self.max_rows = max_rows
        self.commit_every = max(1, int(commit_every))
        self.purge_before_run = purge
        self.search_url = (search_url or "").strip() or None

        self._records: dict[str, LostArtRecord] = {}
        self._processed = 0
        self._anti_bot_warning_shown = False

        self._lostart_cookies = self._load_lostart_cookies(
            anubis_cookie_verification=anubis_cookie_verification,
            anubis_auth=anubis_auth,
            cookie_header=cookie_header,
        )

        self._search_session = requests.Session()
        self._search_session.headers.update(
            {
                "User-Agent": lostart_detail_scraper.SESSION.headers.get(
                    "User-Agent", "Mozilla/5.0"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

        self._apply_cookies_to_session(self._search_session)
        self._apply_cookies_to_session(lostart_detail_scraper.SESSION)
        self._apply_cookies_to_session(lostart_image_scraper.SESSION)

    def run(self, *, skip: int = 0, report_every: int = 10) -> None:
        with image_storage_lock(self.images_dir, exclusive=False):
            self._run_with_image_storage_locked(
                skip=skip,
                report_every=report_every,
            )

    def _run_with_image_storage_locked(
        self,
        *,
        skip: int,
        report_every: int,
    ) -> None:
        if self.purge_before_run:
            with self.db:
                self._purge_existing_data()

        urls = list(self.get_urls(skip=skip))
        report_every = max(1, int(report_every))
        self.stats = {"urls_total": len(urls), "urls_processed": 0}
        self._publish_progress()

        if self.db.session is not None:
            for url in self._iter_urls_with_eta(urls, report_every=report_every):
                self._persist(self.scrape_url(url))
            self._commit_remaining()
            return

        with self.db:
            for url in self._iter_urls_with_eta(urls, report_every=report_every):
                self._persist(self.scrape_url(url))
            self._commit_remaining()

    def get_urls(self, skip: int) -> Iterable[str]:
        if self.search_url:
            return self._get_urls_from_single_search_page(skip=skip)

        urls: list[str] = []
        start = self.start_offset
        pages_fetched = 0
        seen: set[str] = set()
        yielded = 0

        while True:
            if self.max_pages is not None and pages_fetched >= self.max_pages:
                break

            page_url = self._search_page_url(start)
            html = self._fetch_search_page(page_url)
            if not html:
                break

            links = self._extract_result_links(html, base_url=page_url)
            if not links:
                break

            new_on_page = 0
            for link in links:
                if link in seen:
                    continue
                seen.add(link)
                new_on_page += 1
                yielded += 1
                if yielded <= skip:
                    continue
                if self.max_rows is not None and len(urls) >= self.max_rows:
                    break

                lost_art_id = self._id_from_url(link)
                if not lost_art_id:
                    continue

                record = LostArtRecord(lost_art_id=lost_art_id, url=link, index_row={})
                self._records[link] = record
                urls.append(link)

            self.log(f"[search start={start}] extracted {new_on_page} new loss links")

            if self.max_rows is not None and len(urls) >= self.max_rows:
                break
            if new_on_page == 0:
                break

            pages_fetched += 1
            start += 20
            if self.page_delay > 0:
                time.sleep(self.page_delay)

        self.log(f"[load] {len(urls)} links from search pages")
        return urls

    def _get_urls_from_single_search_page(self, *, skip: int) -> list[str]:
        if not self.search_url:
            return []

        html = self._fetch_search_page(self.search_url)
        if not html:
            self.log("[load] 0 links from custom search page")
            return []

        links = self._extract_result_links(html, base_url=self.search_url)
        total_unique = len(links)

        urls: list[str] = []
        seen: set[str] = set()
        yielded = 0

        for link in links:
            if link in seen:
                continue
            seen.add(link)
            yielded += 1
            if yielded <= skip:
                continue
            if self.max_rows is not None and len(urls) >= self.max_rows:
                break

            lost_art_id = self._id_from_url(link)
            if not lost_art_id:
                continue

            record = LostArtRecord(lost_art_id=lost_art_id, url=link, index_row={})
            self._records[link] = record
            urls.append(link)

        self.log(
            f"[search custom] extracted {total_unique} unique object links, selected {len(urls)}"
        )
        self.log(f"[load] {len(urls)} links from custom search page")
        return urls

    def scrape_url(self, url: str):
        record = self._records.get(url)
        if record is None:
            lost_art_id = self._id_from_url(url)
            if not lost_art_id:
                return None
            record = LostArtRecord(lost_art_id=lost_art_id, url=url, index_row={})

        details: dict[str, str] = {}
        html = lostart_detail_scraper.fetch_detail_page(record.url)
        if html and self._is_antibot_page(html):
            self._warn_antibot()
            return None

        if html:
            details = lostart_detail_scraper.extract_field_data(html)
            if self.details_sleep > 0:
                time.sleep(self.details_sleep)

        merged = self._merge_fields(record.index_row, details)

        title_list = self._split_list(merged.get("title") or "")
        title_value = title_list[0] if title_list else f"Lost Art {record.lost_art_id}"
        description = merged.get("description")
        provenance = merged.get("provenance")
        dating = merged.get("dating")
        inventory_number = merged.get("inventory_number")
        material = merged.get("material")
        technique = merged.get("technique")
        width = merged.get("width")
        height = merged.get("height")
        depth = merged.get("depth")
        diameter = merged.get("diameter")
        type_of_loss = self._normalize_type_of_loss(merged.get("type_of_loss"))

        if not width and not height and not depth:
            parsed = self._parse_dimensions(merged.get("dimensions"))
            if parsed:
                height = height or parsed.get("height")
                width = width or parsed.get("width")
                depth = depth or parsed.get("depth")
                diameter = diameter or parsed.get("diameter")

        artist_ids = self._get_artist_ids(
            merged.get("artist"),
            birth_field=merged.get("birth"),
            death_field=merged.get("death"),
        )

        keywords = self._build_keywords(merged)
        raw_data = self._build_raw_data(record.index_row, details)

        institution_id = None
        contact_val = merged.get("contact")
        if contact_val:
            parsed_contact = self._parse_contact_string(contact_val)
            preferred_name = parsed_contact.get("name") or contact_val
            institution = self.db.get_or_create_institution(
                name=self._truncate(preferred_name, 255),
                address=self._truncate(parsed_contact.get("address") or "", 255)
                or None,
                phone=self._truncate(parsed_contact.get("phone") or "", 50) or None,
                email=self._truncate(parsed_contact.get("email") or "", 255) or None,
                website=self._truncate(parsed_contact.get("website") or "", 255)
                or None,
                raw_data=json.dumps(
                    {
                        "raw": contact_val,
                        **{k: v for k, v in parsed_contact.items() if v},
                    },
                    ensure_ascii=False,
                ),
            )
            institution_id = institution.institution_id
        else:
            institution_name = merged.get("institution")
            if institution_name:
                institution = self.db.get_or_create_institution(
                    name=self._truncate(institution_name, 255)
                )
                institution_id = institution.institution_id

        literature_source_id = None
        literature_title = merged.get("literature")
        if literature_title:
            literature = self.db.get_or_create_literature_source(
                title=self._truncate(literature_title, 255),
                raw_data=json.dumps({"raw": literature_title}, ensure_ascii=False),
            )
            literature_source_id = literature.literature_id

        img_paths: list[str] = []
        if self.download_images_enabled:
            try:
                image_urls = lostart_image_scraper.fetch_gallery_sources(record.url)
                img_paths = super().download_images(
                    image_urls,
                    self.images_dir,
                    safe_image_prefix("lostart", record.lost_art_id),
                )
            except Exception as exc:
                self.log(f"[fail] download images for {record.lost_art_id}: {exc}")

        artwork = self.db.upsert_lost_artwork(
            lost_art_id=record.lost_art_id,
            title=title_value,
            artist_ids=artist_ids,
            width=self._parse_float(width),
            height=self._parse_float(height),
            depth=depth,
            diameter=diameter,
            dating=dating,
            description=description,
            provenance=provenance,
            inventory_number=inventory_number,
            material=material,
            technique=technique,
            institution_id=institution_id,
            type_of_loss=type_of_loss,
            lost_art_url=record.url,
            keywords=keywords,
            literature_source_id=literature_source_id,
            raw_data=json.dumps(raw_data, ensure_ascii=False),
        )
        self.db.set_lost_artwork_images(
            lost_artwork_id=artwork.lost_artwork_id,
            image_paths=img_paths,
            image_source_urls=self.last_downloaded_image_sources,
            image_content_sha256=self.last_downloaded_image_content_sha256,
        )

        self._processed += 1
        if self._processed % self.commit_every == 0:
            self._commit_batch()
            self.log(f"[commit] {self._processed} records so far")

        return None

    def _commit_remaining(self) -> None:
        pending = self._processed % self.commit_every
        if pending:
            self._commit_batch()
            self.log(f"[commit] remaining {pending} records")

    def _merge_fields(
        self, index_row: dict[str, str], details: dict[str, str]
    ) -> dict[str, Optional[str]]:
        def pick(keys: list[str]) -> Optional[str]:
            for key in keys:
                val = details.get(key) or index_row.get(key)
                if val:
                    return val.strip()
            return None

        material = pick(["Material", "Material / Technik", "Material / Technique"])
        technique = pick(["Technik", "Technique"])
        if material and not technique and "/" in material:
            left, right = material.split("/", 1)
            if left.strip() and right.strip():
                material = left.strip()
                technique = right.strip()

        return {
            "title": pick(["Titel", "Title"]),
            "artist": pick(
                [
                    "Hersteller/Künstler:in",
                    "Hersteller/Künstler/Autor:in",
                    "Hersteller/Künstler/Autor",
                    "Hersteller/Künstler",
                    "Artist / Creator",
                    "Artist",
                    "Creator",
                ]
            ),
            "description": pick(
                [
                    "Beschreibung",
                    "Beschreibung (DE)",
                    "Description",
                    "Description (EN)",
                ]
            ),
            "provenance": pick(["Provenienz", "Provenance"]),
            "dating": pick(["Datierung", "Dating"]),
            "inventory_number": pick(
                [
                    "Inventarnummer",
                    "Inventarnummer/Signatur",
                    "Inventory number",
                    "Inventory number / Signature",
                ]
            ),
            "material": material,
            "technique": technique,
            "height": pick(["Höhe", "Hoehe", "Höhe (cm)", "Height", "Height (cm)"]),
            "width": pick(["Breite", "Breite (cm)", "Width", "Width (cm)"]),
            "depth": pick(["Tiefe", "Tiefe (cm)", "Depth", "Depth (cm)"]),
            "diameter": pick(
                ["Durchmesser", "Durchmesser (cm)", "Diameter", "Diameter (cm)"]
            ),
            "dimensions": pick(["Abmessungen", "Dimensions"]),
            "type_of_loss": pick(["Meldungsart", "Datensatzart", "Type of report"]),
            "object_type": pick(["Objektart", "Object type"]),
            "object_group": pick(["Objektgruppe", "Object group"]),
            "institution": pick(["Bestand", "Collection"]),
            "contact": pick(["Kontakt", "Kontakt (DE)", "Contact", "Contact (EN)"]),
            "tags": pick(["Schlagworte", "Schlagworte (DE)", "Keywords"]),
            "birth": pick(["Geburt", "Birth"]),
            "death": pick(["Tod", "Death"]),
            "literature": pick(
                [
                    "Literatur / Quelle",
                    "Literatur/Quelle",
                    "Literature / Source",
                    "Literature/Source",
                ]
            ),
        }

    def _get_artist_ids(
        self,
        artist_field: Optional[str],
        *,
        birth_field: Optional[str] = None,
        death_field: Optional[str] = None,
    ) -> list[UUID]:
        if not artist_field:
            return []

        birth_date = self._parse_year_to_date(birth_field)
        death_date = self._parse_year_to_date(death_field)

        parts = self._split_list(artist_field)
        ids: list[UUID] = []
        for raw in parts:
            cleaned = self._clean_artist_name(raw)
            if not cleaned:
                continue
            first_name, last_name = self._split_name(cleaned)
            artist = self.db.get_or_create_artist(
                last_name=last_name,
                first_name=first_name,
                date_of_birth=birth_date,
                date_of_death=death_date,
                raw_data=json.dumps(
                    {"raw": raw, "birth": birth_field, "death": death_field},
                    ensure_ascii=False,
                ),
            )
            ids.append(artist.artist_id)
        return ids

    @staticmethod
    def _split_list(value: str) -> list[str]:
        if not value:
            return []
        items = [item.strip() for item in re.split(r";|\|", value) if item.strip()]
        return items

    @staticmethod
    def _clean_artist_name(value: str) -> Optional[str]:
        if not value:
            return None
        cleaned = re.sub(r"\[[^\]]+\]", "", value)
        cleaned = re.sub(r"\([^\)]*\)", "", cleaned)
        cleaned = " ".join(cleaned.split()).strip()
        if not cleaned:
            return None
        if cleaned.lower() in {"unbekannt", "unknown"}:
            return None
        return cleaned

    @staticmethod
    def _split_name(value: str) -> tuple[Optional[str], str]:
        if "," in value:
            last, first = [part.strip() for part in value.split(",", 1)]
            return (first or None), (last or value)
        return None, value

    @staticmethod
    def _id_from_url(url: str) -> Optional[str]:
        if not url:
            return None
        parsed = urlsplit(url)
        return parsed.path.rstrip("/").split("/")[-1]

    @staticmethod
    def _truncate(value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[: max_len - 1]

    @staticmethod
    def _parse_dimensions(raw: Optional[str]) -> Optional[dict[str, str]]:
        if not raw:
            return None
        cleaned = raw.replace("×", "x")
        parts = [p.strip() for p in cleaned.split("x") if p.strip()]
        if len(parts) < 2:
            return None
        values = [p for p in parts if p]
        result: dict[str, str] = {}
        if len(values) >= 2:
            result["height"] = values[0]
            result["width"] = values[1]
        if len(values) >= 3:
            result["depth"] = values[2]
        return result

    @staticmethod
    def _parse_float(raw: Optional[str]) -> Optional[float]:
        if not raw:
            return None
        normalized = raw.replace(",", ".")
        match = re.search(r"-?\d+(?:\.\d+)?", normalized)
        if not match:
            return None
        try:
            return float(match.group(0))
        except Exception:
            return None

    @staticmethod
    def _search_page_url(start: int) -> str:
        return f"https://www.lostart.de/de/suche?filter%5Btype%5D%5B0%5D=Objektdaten&start={start}"

    @staticmethod
    def _extract_result_links(html: str, *, base_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        seen: set[str] = set()

        valid_tokens = (
            "/verlust/objekt/",
            "/loss/object/",
            "/found/object/",
        )

        def maybe_add(raw_url: str) -> None:
            if not raw_url:
                return
            url_l = raw_url.lower()
            if not any(token in url_l for token in valid_tokens):
                return
            absolute = urljoin(base_url, raw_url)
            parsed = urlsplit(absolute)
            canonical = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            if canonical in seen:
                return
            seen.add(canonical)
            links.append(canonical)

        for a in soup.find_all("a", href=True):
            maybe_add(a.get("href") or "")

        for node in soup.find_all(attrs={"data-result-url": True}):
            maybe_add((node.get("data-result-url") or "").strip())

        return links

    def _fetch_search_page(self, page_url: str) -> str:
        try:
            response = self._search_session.get(page_url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            self.log(f"[fail] fetch search page {page_url}: {exc}")
            return ""

        html = response.text or ""
        if self._is_antibot_page(html):
            self._warn_antibot()
        return html

    @classmethod
    def _load_lostart_env_file(cls) -> None:
        if cls._env_loaded:
            return

        env_path = Path(__file__).resolve().parent / ".env"
        if not env_path.exists() or not env_path.is_file():
            cls._env_loaded = True
            return

        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)

        cls._env_loaded = True

    @staticmethod
    def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
        cookies: dict[str, str] = {}
        for part in cookie_header.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            value = value.strip()
            if name and value:
                cookies[name] = value
        return cookies

    @classmethod
    def _load_lostart_cookies(
        cls,
        *,
        anubis_cookie_verification: Optional[str] = None,
        anubis_auth: Optional[str] = None,
        cookie_header: Optional[str] = None,
    ) -> dict[str, str]:
        cookies: dict[str, str] = {}

        raw_cookie_header = (
            cookie_header or os.getenv("LOSTART_COOKIE_HEADER") or ""
        ).strip()
        if raw_cookie_header:
            cookies.update(cls._parse_cookie_header(raw_cookie_header))

        cookie_verification = (
            anubis_cookie_verification
            or os.getenv("LOSTART_ANUBIS_COOKIE_VERIFICATION")
            or ""
        ).strip()
        anubis_auth_token = (
            anubis_auth or os.getenv("LOSTART_ANUBIS_AUTH") or ""
        ).strip()

        if cookie_verification:
            cookies["techaro.lol-anubis-cookie-verification"] = cookie_verification
        if anubis_auth_token:
            cookies["techaro.lol-anubis-auth"] = anubis_auth_token

        return cookies

    def _apply_cookies_to_session(self, session: requests.Session) -> None:
        if not self._lostart_cookies:
            return
        for name, value in self._lostart_cookies.items():
            session.cookies.set(name, value, domain="www.lostart.de", path="/")
            session.cookies.set(name, value, domain=".lostart.de", path="/")

    @staticmethod
    def _is_antibot_page(html: str) -> bool:
        lowered = (html or "").lower()
        return (
            "anubis_challenge" in lowered
            or "botstopper" in lowered
            or "making sure you&#39;re not a bot" in lowered
            or "making sure you're not a bot" in lowered
        )

    def _warn_antibot(self) -> None:
        if self._anti_bot_warning_shown:
            return
        self._anti_bot_warning_shown = True
        self.log(
            "[botstopper] challenge detected; "
            "provide valid cookies via LOSTART_ANUBIS_COOKIE_VERIFICATION and "
            "LOSTART_ANUBIS_AUTH (or LOSTART_COOKIE_HEADER / CLI flags)"
        )

    def _build_keywords(self, merged: dict[str, Optional[str]]) -> list[str]:
        """Prefer explicit tag field (Schlagworte) if present, otherwise fallback."""
        keywords: list[str] = []

        tags_val = merged.get("tags") or merged.get("Schlagworte")
        if tags_val:
            for item in self._split_list(tags_val):
                if item and item not in keywords:
                    keywords.append(item)
            return keywords

        for key in ["object_type", "object_group", "institution", "type_of_loss"]:
            value = merged.get(key)
            if not value:
                continue
            for item in self._split_list(value):
                if item and item not in keywords:
                    keywords.append(item)
        return keywords

    def _build_raw_data(
        self, index_row: dict[str, str], details: dict[str, str]
    ) -> dict[str, str]:
        raw: dict[str, str] = {}
        for source in (index_row, details):
            for key, value in source.items():
                if not value:
                    continue
                raw[key] = value
        return raw

    def _parse_contact_string(self, text: str) -> dict[str, Optional[str]]:
        """Parse Kontakt string into name, address, phone, email, website.

        This is heuristic and defensive — it looks for tokens separated by ';' and
        pulls email, phone and website. Remaining leading tokens are used as name/address.
        """
        tokens = [t.strip() for t in re.split(r";|\n", text) if t.strip()]
        email = None
        phone = None
        website = None
        name_parts: list[str] = []
        address_parts: list[str] = []

        i = 0
        while i < len(tokens):
            tok = tokens[i]
            tl = tok.lower()
            if "@" in tok and not email:
                email = tok
                i += 1
                continue
            if tok.startswith("http://") or tok.startswith("https://"):
                website = tok
                i += 1
                continue
            if tl in {"tel", "telefon", "phone"} and i + 1 < len(tokens):
                candidate = tokens[i + 1]
                phone = candidate
                i += 2
                continue
            if tl in {"e-mail", "e mail", "email"} and i + 1 < len(tokens):
                candidate = tokens[i + 1]
                email = candidate
                i += 2
                continue
            if tl in {"homepage", "homepage:"} and i + 1 < len(tokens):
                candidate = tokens[i + 1]
                website = candidate
                i += 2
                continue

            if re.search(r"\d{5}", tok) or re.search(
                r"straße|str\.|talen|platz|road|rd\b|avenue|addr", tl
            ):
                address_parts.append(tok)
            else:
                name_parts.append(tok)
            i += 1

        name = "; ".join(name_parts) if name_parts else None
        address = "; ".join(address_parts) if address_parts else None
        return {
            "name": name,
            "address": address,
            "phone": phone,
            "email": email,
            "website": website,
        }

    def _parse_year_to_date(self, text: Optional[str]):
        if not text:
            return None
        m = re.search(r"(1\d{3}|20\d{2})", text)
        if not m:
            return None
        try:
            y = int(m.group(1))
            from datetime import date

            return date(y, 1, 1)
        except Exception:
            return None

    def _normalize_type_of_loss(self, raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        parts = self._split_list(raw)
        if parts:
            return parts[0]
        cleaned = raw.strip()
        return cleaned or None

    def _purge_existing_data(self) -> None:
        session = self.db._get_session()
        deleted = session.query(LostArtwork).delete(synchronize_session=False)
        self.log(f"[purge] removed {deleted} existing artworks")
