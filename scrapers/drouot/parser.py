from __future__ import annotations

from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .artist import DrouotArtistMixin
from .auctioneer import DrouotAuctioneerMixin
from .constants import BASE_URL, LOT_PATH_RE, PARSER
from .description import DrouotDescriptionMixin
from .js_extract import DrouotJsExtractMixin
from .models import DrouotLot
from .page_meta import DrouotPageMetaMixin


class DrouotLotParser(
    DrouotArtistMixin,
    DrouotDescriptionMixin,
    DrouotAuctioneerMixin,
    DrouotJsExtractMixin,
    DrouotPageMetaMixin,
):
    def __init__(self, *, log=print) -> None:
        # ``log`` is the bound ``Scraper.log`` of the owning scraper, so
        # parser-side messages share the ``[<id>]`` prefix automatically.
        self._log = log

    def extract_lot_urls(self, html: str) -> list[str]:
        soup = BeautifulSoup(html, PARSER)
        out: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", "")).strip()
            if not LOT_PATH_RE.match(href):
                continue
            out.append(urljoin(BASE_URL, href))
        return out

    def parse_lot_page(self, *, html: str, lot_url: str) -> Optional[DrouotLot]:
        try:
            soup = BeautifulSoup(html, PARSER)
            lot_object = self._extract_js_object(html, "lot:{")
            lot_id = self._extract_lot_id(lot_url) or self._extract_js_number_as_str(lot_object, "id") or ""
            if not lot_id:
                self._log(f"[fail] could not determine lot_id for {lot_url}")
                return None

            raw_heading = self._extract_title(soup, lot_url)
            description_from_dom, description = self._extract_description(soup=soup, lot_object=lot_object)
            parsed_title, artist_name = self._resolve_title_and_artist(
                raw_heading=raw_heading,
                description=description,
                description_from_dom=description_from_dom,
                lot_object=lot_object,
                lot_url=lot_url,
            )

            pricing = self._extract_pricing_fields(lot_object)
            auction_timing = self._extract_auction_timing_fields(soup=soup, lot_object=lot_object)
            auctioneer = self._extract_auctioneer_fields(soup=soup, lot_object=lot_object)
            categories = self._extract_category_fields(lot_object)
            sale_state = self._extract_sale_state_fields(lot_object)

            catalogue_name = self._extract_catalogue_name(soup) or self._extract_js_string(lot_object, "saleSlug")
            product_schema = self._extract_product_schema(soup)
            raw_data = self._build_raw_payload(
                lot_url=lot_url,
                lot_object=lot_object,
                product_schema=product_schema,
                display_heading=raw_heading,
            )

            return DrouotLot(
                lot_id=lot_id,
                lot_number=pricing["lot_number"],
                title=parsed_title,
                artist_name=artist_name,
                description=description,
                image_urls=self._extract_image_urls(lot_object),
                start_price=pricing["start_price"],
                estimate_low=pricing["estimate_low"],
                estimate_high=pricing["estimate_high"],
                currency=pricing["currency"],
                buyer_fees_percent=pricing["buyer_fees_percent"],
                auction_date=auction_timing["auction_date"],
                auction_date_text=auction_timing["auction_date_text"],
                auction_timezone=auction_timing["auction_timezone"],
                auction_location=auctioneer["auctioneer_address"],
                auctioneer_name=auctioneer["auctioneer_name"],
                auctioneer_url=auctioneer["auctioneer_url"],
                auctioneer_phone=auctioneer["auctioneer_phone"],
                auctioneer_email=auctioneer["auctioneer_email"],
                auctioneer_address=auctioneer["auctioneer_address"],
                catalogue_name=catalogue_name,
                lot_category=categories["lot_category"],
                lot_primary_category=categories["lot_primary_category"],
                auction_city=auction_timing["auction_city"],
                auction_country=auction_timing["auction_country"],
                lot_start_date=auction_timing["lot_start_date"],
                lot_end_date=auction_timing["lot_end_date"],
                bidding_type=sale_state["bidding_type"],
                auction_accepts_bids=sale_state["auction_accepts_bids"],
                auction_goes_live=sale_state["auction_goes_live"],
                auction_published=sale_state["auction_published"],
                auction_closed=sale_state["auction_closed"],
                raw_data=raw_data,
            )
        except Exception as exc:
            self._log(f"[fail] parse lot page {lot_url}: {exc}")
            return None

    def _extract_description(self, *, soup: BeautifulSoup, lot_object: str) -> tuple[str, str]:
        description_from_dom = self._extract_description_from_dom(soup)
        description_from_js = self._extract_js_string(
            lot_object,
            "description",
            collapse_whitespace=False,
        )
        description = description_from_dom or description_from_js or ""
        return description_from_dom, description

    def _resolve_title_and_artist(
        self,
        *,
        raw_heading: str,
        description: str,
        description_from_dom: str,
        lot_object: str,
        lot_url: str,
    ) -> tuple[Optional[str], Optional[str]]:
        parsed_title = self._clean_display_title(raw_heading)
        if self._is_placeholder_title(parsed_title) or self._is_low_quality_title(parsed_title):
            parsed_title = None

        artist_name = self._normalize_explicit_artist(
            self._extract_js_string(lot_object, "artistName")
            or self._extract_js_string(lot_object, "authorName")
            or self._extract_js_string(lot_object, "author")
        )
        return parsed_title, artist_name

    def _extract_pricing_fields(self, lot_object: str) -> dict[str, Optional[float | str]]:
        buyer_fees_percent = self._extract_js_number(lot_object, "fees")
        if buyer_fees_percent is None:
            buyer_fees_percent = self._extract_js_number(lot_object, "saleFees")

        return {
            "lot_number": self._extract_js_number_as_str(lot_object, "num"),
            "estimate_low": self._extract_js_number(lot_object, "lowEstim"),
            "estimate_high": self._extract_js_number(lot_object, "highEstim"),
            "start_price": self._extract_js_number(lot_object, "nextBid"),
            "buyer_fees_percent": buyer_fees_percent,
            "currency": self._extract_js_string(lot_object, "currencyId"),
        }

    def _extract_auction_timing_fields(self, *, soup: BeautifulSoup, lot_object: str) -> dict[str, Optional[object]]:
        auction_date, auction_timezone = self._extract_auction_date(lot_object)
        return {
            "auction_date": auction_date,
            "auction_timezone": auction_timezone,
            "auction_date_text": self._extract_live_date_text(soup),
            "auction_city": self._extract_js_string(lot_object, "city"),
            "auction_country": self._extract_js_string(lot_object, "country"),
            "lot_start_date": self._extract_epoch_key_as_iso(lot_object, "date"),
            "lot_end_date": self._extract_epoch_key_as_iso(lot_object, "bidEndDate"),
        }

    def _extract_auctioneer_fields(self, *, soup: BeautifulSoup, lot_object: str) -> dict[str, Optional[str]]:
        auctioneer_address = self._extract_auctioneer_address(soup, lot_object)
        return {
            "auctioneer_name": self._extract_auctioneer_name(soup, lot_object),
            "auctioneer_url": self._extract_auctioneer_url(soup),
            "auctioneer_phone": self._extract_auctioneer_phone(soup, lot_object),
            "auctioneer_email": self._extract_auctioneer_email(soup, lot_object),
            "auctioneer_address": auctioneer_address,
        }

    def _extract_category_fields(self, lot_object: str) -> dict[str, Optional[str]]:
        category_values = self._extract_js_number_list(lot_object, "categories")
        return {
            "lot_category": ", ".join(str(value) for value in category_values) if category_values else None,
            "lot_primary_category": str(category_values[0]) if category_values else None,
        }

    def _extract_sale_state_fields(self, lot_object: str) -> dict[str, Optional[bool | str]]:
        bidding_type = self._extract_js_string(lot_object, "saleType")
        sale_status = self._extract_js_string(lot_object, "saleStatus")
        return {
            "bidding_type": bidding_type,
            "auction_accepts_bids": self._extract_js_bool(lot_object, "autobidActive"),
            "auction_goes_live": bidding_type.upper() == "LIVE" if bidding_type else None,
            "auction_published": sale_status.upper() in {"CREATED", "PUBLISHED", "OPEN"} if sale_status else None,
            "auction_closed": sale_status.upper() in {"CLOSED", "ENDED", "FINISHED"} if sale_status else None,
        }
