from __future__ import annotations

import json
from dataclasses import asdict
from typing import Optional

from bs4 import BeautifulSoup

from .constants import PARSER
from .detail_extract import DorotheumDetailExtractMixin
from .detail_meta import DorotheumDetailMetaMixin
from .models import DorotheumListingLot, DorotheumLot


class DorotheumDetailMixin(DorotheumDetailExtractMixin, DorotheumDetailMetaMixin):
    def parse_lot_page(
        self,
        *,
        html: str,
        lot_url: str,
        listing_lot: Optional[DorotheumListingLot] = None,
    ) -> Optional[DorotheumLot]:
        soup = BeautifulSoup(html, PARSER)
        lot_uid = self._extract_lot_uid(lot_url)
        if not lot_uid:
            return None

        fields = self._collect_lot_fields(soup=soup, lot_url=lot_url, lot_uid=lot_uid, listing_lot=listing_lot)
        return DorotheumLot(**fields)

    def _collect_lot_fields(
        self,
        *,
        soup: BeautifulSoup,
        lot_url: str,
        lot_uid: str,
        listing_lot: Optional[DorotheumListingLot],
    ) -> dict[str, object]:
        lot_status_attrs, ga4_tracking = self._extract_status_and_tracking(soup)
        lot_number, title, artist_name = self._resolve_identity(
            soup=soup,
            ga4_tracking=ga4_tracking,
            listing_lot=listing_lot,
        )

        description = self._extract_description(soup) or (listing_lot.description if listing_lot else "")
        image_urls = self._extract_image_urls(soup) or (listing_lot.image_urls if listing_lot else [])
        auction_fields, auction_table = self._resolve_auction_bundle(
            soup=soup,
            ga4_tracking=ga4_tracking,
            lot_status_attrs=lot_status_attrs,
            listing_lot=listing_lot,
        )

        expert_name, expert_phone, expert_email = self._extract_expert(soup)
        return {
            "lot_uid": lot_uid,
            "lot_id": lot_number,
            "lot_url": lot_url,
            "title": title,
            "artist_name": artist_name,
            "description": description,
            "image_urls": image_urls,
            **auction_fields,
            "expert_name": expert_name,
            "expert_phone": expert_phone,
            "expert_email": expert_email,
            "raw_data": self._build_raw_payload(
                lot_url=lot_url,
                lot_uid=lot_uid,
                listing_lot=listing_lot,
                lot_status_attrs=lot_status_attrs,
                ga4_tracking=ga4_tracking,
                auction_table=auction_table,
                soup=soup,
            ),
        }

    def _resolve_auction_bundle(
        self,
        *,
        soup: BeautifulSoup,
        ga4_tracking: dict[str, object],
        lot_status_attrs: dict[str, object],
        listing_lot: Optional[DorotheumListingLot],
    ) -> tuple[dict[str, object], dict[str, str]]:
        auction_date, auction_date_text = self._extract_auction_date(soup)
        if auction_date is None and listing_lot is not None:
            auction_date = listing_lot.auction_date

        auction_table = self._extract_auction_table(soup)
        auction_name, auction_type, auction_location, lot_category = self._resolve_auction_fields(
            auction_table=auction_table,
            ga4_tracking=ga4_tracking,
            listing_lot=listing_lot,
        )
        start_price, estimate_low, estimate_high, currency = self._resolve_pricing_fields(
            soup=soup,
            lot_status_attrs=lot_status_attrs,
            ga4_tracking=ga4_tracking,
            listing_lot=listing_lot,
        )

        return {
            "auction_date": auction_date,
            "auction_date_text": auction_date_text,
            "auction_name": auction_name,
            "auction_type": auction_type,
            "auction_location": auction_location,
            "lot_category": lot_category,
            "start_price": start_price,
            "estimate_low": estimate_low,
            "estimate_high": estimate_high,
            "currency": currency,
        }, auction_table

    def _resolve_identity(
        self,
        *,
        soup: BeautifulSoup,
        ga4_tracking: dict[str, object],
        listing_lot: Optional[DorotheumListingLot],
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        lot_number = self._extract_lot_number(soup) or self._pick_text(
            ga4_tracking.get("lot_id"),
            listing_lot.lot_id if listing_lot else None,
        )

        artist_name = self._clean_artist_name(
            self._pick_text(
                listing_lot.artist_name if listing_lot else None,
                ga4_tracking.get("lot_artist"),
            )
        )

        title = self._normalize_title(
            self._pick_text(
                self._extract_title(soup),
                listing_lot.title if listing_lot else None,
                ga4_tracking.get("lot_name"),
            ),
            artist_name=artist_name,
        )

        return lot_number, title, artist_name

    def _resolve_auction_fields(
        self,
        *,
        auction_table: dict[str, str],
        ga4_tracking: dict[str, object],
        listing_lot: Optional[DorotheumListingLot],
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        auction_name = self._pick_text(
            auction_table.get("Auktion"),
            ga4_tracking.get("auction_name"),
            listing_lot.auction_name if listing_lot else None,
        )
        auction_type = self._pick_text(
            auction_table.get("Auktionstyp"),
            listing_lot.auction_type if listing_lot else None,
        )
        auction_location = self._pick_text(
            auction_table.get("Auktionsort"),
            ga4_tracking.get("auction_location"),
            listing_lot.auction_location if listing_lot else None,
        )
        lot_category = self._pick_text(
            ga4_tracking.get("lot_category"),
            listing_lot.lot_category if listing_lot else None,
        )
        return auction_name, auction_type, auction_location, lot_category

    def _resolve_pricing_fields(
        self,
        *,
        soup: BeautifulSoup,
        lot_status_attrs: dict[str, object],
        ga4_tracking: dict[str, object],
        listing_lot: Optional[DorotheumListingLot],
    ) -> tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
        start_price = self._parse_amount(lot_status_attrs.get("data-rufpreis"))
        if start_price is None:
            start_price = self._extract_amount_by_label(soup, "Startpreis")
        if start_price is None and listing_lot is not None:
            start_price = listing_lot.start_price

        estimate_text = self._extract_value_by_label(soup, "Schätzwert")
        estimate_low, estimate_high = self._parse_estimate_range(estimate_text)

        currency = self._pick_text(
            lot_status_attrs.get("data-currency"),
            ga4_tracking.get("lot_currency"),
            listing_lot.currency if listing_lot else None,
        )
        return start_price, estimate_low, estimate_high, currency

    def _build_raw_payload(
        self,
        *,
        lot_url: str,
        lot_uid: str,
        listing_lot: Optional[DorotheumListingLot],
        lot_status_attrs: dict[str, object],
        ga4_tracking: dict[str, object],
        auction_table: dict[str, str],
        soup: BeautifulSoup,
    ) -> str:
        payload = {
            "source": "dorotheum",
            "lot_url": lot_url,
            "lot_uid": lot_uid,
            "listing": asdict(listing_lot) if listing_lot else None,
            "lot_status": lot_status_attrs,
            "ga4_tracking": ga4_tracking,
            "schema_graph": self._extract_schema_graph(soup),
            "auction_table": auction_table,
            "email_share": self._extract_email_share_data(soup),
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
