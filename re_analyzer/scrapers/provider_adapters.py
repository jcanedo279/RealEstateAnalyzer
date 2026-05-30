from abc import ABC, abstractmethod
from copy import deepcopy
import json
import math
import os
import random
import re
import time
from typing import Iterable, List
from urllib.parse import urlencode

from re_analyzer.scrapers.property_identity import (
    CanonicalListing,
    canonical_property_identity,
    normalize_address,
    normalize_zip_code,
    parse_city_state_zip_from_address,
)
from re_analyzer.utility.utility import PROPERTY_DETAILS_PATH, load_json


def _coerce_number(value):
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value
    text = re.sub(r"[^0-9.]", "", str(value))
    if not text:
        return None
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


def _coerce_int(value):
    number = _coerce_number(value)
    return int(number) if number is not None else None


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    if str(value).strip().lower() in {"true", "1", "yes"}:
        return True
    if str(value).strip().lower() in {"false", "0", "no"}:
        return False
    return None


def _lot_size_sqft(value, unit=""):
    number = _coerce_number(value)
    if number is None:
        return None
    unit_text = str(unit or "").strip().lower()
    if unit_text in {"acre", "acres", "ac"}:
        return round(float(number) * 43560, 2)
    return number


def _compact_dict(data):
    return {key: value for key, value in (data or {}).items() if value not in (None, "", [], {})}


def _safe_first(*values):
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _deep_find_first_number(data, key_fragments):
    key_fragments = tuple(fragment.lower() for fragment in key_fragments)
    if isinstance(data, dict):
        for key, value in data.items():
            key_text = str(key).lower()
            if any(fragment in key_text for fragment in key_fragments):
                number = _coerce_number(value)
                if number is not None:
                    return number
                if isinstance(value, dict):
                    for nested_key in ("value", "amount", "estimate", "estimatedValue", "current"):
                        number = _coerce_number(value.get(nested_key))
                        if number is not None:
                            return number
            if isinstance(value, (dict, list)):
                number = _deep_find_first_number(value, key_fragments)
                if number is not None:
                    return number
    elif isinstance(data, list):
        for item in data:
            number = _deep_find_first_number(item, key_fragments)
            if number is not None:
                return number
    return None


def _deep_find_first_sequence(data, key_fragments):
    key_fragments = tuple(fragment.lower() for fragment in key_fragments)
    if isinstance(data, dict):
        for key, value in data.items():
            key_text = str(key).lower()
            if any(fragment in key_text for fragment in key_fragments) and isinstance(value, list):
                return value
            if isinstance(value, (dict, list)):
                result = _deep_find_first_sequence(value, key_fragments)
                if result:
                    return result
    elif isinstance(data, list):
        for item in data:
            result = _deep_find_first_sequence(item, key_fragments)
            if result:
                return result
    return []


def _compact_history_rows(rows, limit=180):
    compact_rows = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        compact_rows.append(_compact_dict(row))
        if len(compact_rows) >= limit:
            break
    return compact_rows


RENT_KEY_FRAGMENTS = (
    "rentzestimate",
    "restimate",
    "rentestimate",
    "rent_estimate",
    "rentalestimate",
    "rental_estimate",
    "rentalearning",
    "rental_earning",
    "monthlyrent",
    "market_rent",
    "marketrent",
)
VALUE_ESTIMATE_KEY_FRAGMENTS = (
    "zestimate",
    "realestimate",
    "avm",
    "homevalue",
    "home_value",
    "valueestimate",
    "value_estimate",
    "estimatedvalue",
    "estimated_value",
    "automatedvaluation",
    "automated_valuation",
)
RENTAL_SIGNAL_VALUES = (
    "for rent",
    "for_rent",
    "rental",
    "rentals",
    "for lease",
    "for_lease",
    "lease",
    "leased",
)


def _plausible_monthly_rent(value):
    number = _coerce_number(value)
    if number is None:
        return None
    if 500 <= float(number) <= 100000:
        return number
    return None


def _plausible_value_estimate(value):
    number = _coerce_number(value)
    if number is None:
        return None
    if 10000 <= float(number) <= 500000000:
        return number
    return None


def _is_explicit_rental_listing(provider_name, raw_listing, status="", home_type="", url=""):
    status_text = str(status or raw_listing.get("status") or raw_listing.get("statusText") or raw_listing.get("mlsStatus") or "").lower()
    type_text = str(home_type or raw_listing.get("homeStatus") or raw_listing.get("listingType") or raw_listing.get("searchStatus") or "").lower()
    url_text = str(url or raw_listing.get("url") or raw_listing.get("href") or "").lower()
    explicit_text = " ".join([status_text, type_text, url_text])
    if any(signal in explicit_text for signal in RENTAL_SIGNAL_VALUES):
        return True

    if provider_name == "zillow":
        home_info = ((raw_listing.get("hdpData") or {}).get("homeInfo") or {}) if isinstance(raw_listing.get("hdpData"), dict) else {}
        zillow_status = str(raw_listing.get("statusType") or home_info.get("homeStatus") or "").lower()
        return raw_listing.get("isRentalWithBasePrice") is True or "for_rent" in zillow_status or "for rent" in zillow_status
    if provider_name == "realtor":
        flags = raw_listing.get("flags") if isinstance(raw_listing.get("flags"), dict) else {}
        if flags.get("is_for_rent") or flags.get("is_rental"):
            return True
    if provider_name == "redfin":
        return str(raw_listing.get("listingType") or "").upper() in {"RENTAL", "FOR_RENT", "LEASE"}
    return False


def _direct_rent_estimate_from_listing(provider_name, raw_listing, price=None, status="", home_type="", url=""):
    """Use listing price as rent only when the row is explicitly rental inventory."""
    if not _is_explicit_rental_listing(provider_name, raw_listing or {}, status=status, home_type=home_type, url=url):
        return None
    return _plausible_monthly_rent(price)


def _candidate_text_fields(raw_listing):
    fields = []

    def add(value):
        if isinstance(value, str) and value.strip():
            fields.append(value)

    add(raw_listing.get("listingRemarks"))
    add(raw_listing.get("remarks"))
    add(raw_listing.get("propertyDescription"))
    description = raw_listing.get("description")
    if isinstance(description, dict):
        for key in ("text", "text_long", "description", "summary"):
            add(description.get(key))
    else:
        add(description)
    return fields


def _remarks_monthly_rent_estimate(raw_listing):
    candidates = []
    patterns = (
        re.compile(
            r"\b(?:rent estimate|rental estimate|market rent|rent zestimate|restimate)[^$.0-9]{0,80}"
            r"\$?\s*(\d{1,3}(?:,\d{3})+|\d{3,6})(?:\.\d+)?\s*(?:/|per)?\s*(?:mo|month|monthly)?",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:rent|rental|lease|leased|rented)[^$.0-9]{0,80}"
            r"\$?\s*(\d{1,3}(?:,\d{3})+|\d{3,6})(?:\.\d+)?\s*(?:/|per)?\s*(?:mo|month|monthly)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\$?\s*(\d{1,3}(?:,\d{3})+|\d{3,6})(?:\.\d+)?\s*(?:/|per)?\s*(?:mo|month|monthly)"
            r"[^.]{0,80}\b(?:rent|rental|lease)\b",
            re.IGNORECASE,
        ),
    )
    for text in _candidate_text_fields(raw_listing or {}):
        normalized = text.replace("\xa0", " ")
        for pattern in patterns:
            for match in pattern.finditer(normalized):
                rent = _plausible_monthly_rent(match.group(1))
                if rent is not None:
                    candidates.append(rent)
    return candidates[0] if candidates else None


def _estimate_key_audit(raw_listing, max_hits=12):
    raw_listing = raw_listing or {}
    hits = {"rent": [], "value": []}

    def visit(value, path=""):
        if len(hits["rent"]) >= max_hits and len(hits["value"]) >= max_hits:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key).lower()
                item_path = f"{path}.{key}" if path else str(key)
                if any(fragment in key_text for fragment in RENT_KEY_FRAGMENTS) and len(hits["rent"]) < max_hits:
                    rent = _plausible_monthly_rent(item)
                    if rent is not None:
                        hits["rent"].append({"path": item_path, "value": rent})
                if any(fragment in key_text for fragment in VALUE_ESTIMATE_KEY_FRAGMENTS) and len(hits["value"]) < max_hits:
                    value_estimate = _plausible_value_estimate(item)
                    if value_estimate is not None:
                        hits["value"].append({"path": item_path, "value": value_estimate})
                if isinstance(item, (dict, list)):
                    visit(item, item_path)
        elif isinstance(value, list):
            for index, item in enumerate(value[:100]):
                if isinstance(item, (dict, list)):
                    visit(item, f"{path}[{index}]")

    visit(raw_listing)
    return {
        "rent_key_hits": hits["rent"],
        "value_key_hits": hits["value"],
    }


def _load_zillow_detail_property(zip_code, zpid):
    if not zip_code or not zpid:
        return {}, []
    path = os.path.join(PROPERTY_DETAILS_PATH, str(zip_code), f"{zpid}_property_details.json")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}, []
    details = load_json(path)
    if not isinstance(details, dict) or "props" not in details:
        return {}, []
    component_props = ((details.get("props") or {}).get("pageProps") or {}).get("componentProps") or {}
    property_data = component_props.get("gdp") or component_props.get("gdpClientCache")
    if isinstance(property_data, str):
        try:
            property_data = json.loads(property_data)
        except json.JSONDecodeError:
            property_data = {}
    if not isinstance(property_data, dict) or not property_data:
        return {}, details.get("zestimateHistory") or []
    first_value = next(iter(property_data.values()), {})
    return (first_value or {}).get("property") or {}, details.get("zestimateHistory") or []


def _zillow_provider_metadata(raw_listing, detail_property):
    reso_facts = detail_property.get("resoFacts") if isinstance(detail_property.get("resoFacts"), dict) else {}
    return _compact_dict({
        "broker_name": _safe_first(raw_listing.get("brokerName"), detail_property.get("brokerageName")),
        "builder_name": _safe_first(raw_listing.get("builderName"), detail_property.get("builderName")),
        "marketing_status": raw_listing.get("marketingStatusSimplifiedCd"),
        "raw_home_status": raw_listing.get("rawHomeStatusCd"),
        "listing_type": detail_property.get("listingTypeDimension"),
        "listing_sub_type": detail_property.get("listingSubType"),
        "property_type_dimension": detail_property.get("propertyTypeDimension"),
        "days_on_zillow": detail_property.get("daysOnZillow"),
        "time_on_zillow": detail_property.get("timeOnZillow"),
        "monthly_hoa_fee": detail_property.get("monthlyHoaFee"),
        "property_tax_rate": detail_property.get("propertyTaxRate"),
        "annual_homeowners_insurance": detail_property.get("annualHomeownersInsurance"),
        "favorite_count": detail_property.get("favoriteCount"),
        "page_view_count": detail_property.get("pageViewCount"),
        "photo_count": detail_property.get("photoCount"),
        "date_posted": detail_property.get("datePostedString"),
        "is_undisclosed_address": _coerce_bool(raw_listing.get("isUndisclosedAddress") or detail_property.get("isUndisclosedAddress")),
        "is_zillow_owned": _coerce_bool(raw_listing.get("isZillowOwned") or detail_property.get("isZillowOwned")),
        "is_showcase_listing": _coerce_bool(raw_listing.get("isShowcaseListing") or detail_property.get("isShowcaseListing")),
        "is_paid_builder_new_construction": _coerce_bool(raw_listing.get("isPaidBuilderNewConstruction") or detail_property.get("isPremierBuilder")),
        "reso_facts": _compact_dict({
            "association_fee": reso_facts.get("associationFee"),
            "association_amenities": reso_facts.get("associationAmenities"),
            "has_pool": reso_facts.get("hasPrivatePool"),
            "has_waterfront_view": reso_facts.get("hasWaterfrontView"),
            "parking_features": reso_facts.get("parkingFeatures"),
            "garage_spaces": reso_facts.get("garageSpaces"),
            "cooling": reso_facts.get("cooling"),
            "heating": reso_facts.get("heating"),
            "flooring": reso_facts.get("flooring"),
            "roof_type": reso_facts.get("roofType"),
            "stories": reso_facts.get("stories"),
            "appliances": reso_facts.get("appliances"),
            "subdivision_name": reso_facts.get("subdivisionName"),
        }),
    })


def _redfin_provider_metadata(raw_listing):
    return _compact_dict({
        "mls_id": _dict_or_value(raw_listing.get("mlsId")),
        "mls_status": raw_listing.get("mlsStatus"),
        "search_status": raw_listing.get("searchStatus"),
        "listing_type": raw_listing.get("listingType"),
        "property_type": raw_listing.get("propertyType"),
        "price_per_sqft": _coerce_number(raw_listing.get("pricePerSqFt")),
        "hoa": _coerce_number(raw_listing.get("hoa")),
        "dom": _coerce_number(raw_listing.get("dom")),
        "time_on_redfin": _coerce_number(raw_listing.get("timeOnRedfin")),
        "original_time_on_redfin": _coerce_number(raw_listing.get("originalTimeOnRedfin")),
        "sold_date": raw_listing.get("soldDate"),
        "listing_broker": raw_listing.get("listingBroker"),
        "listing_tags": raw_listing.get("listingTags"),
        "key_facts": raw_listing.get("keyFacts"),
        "listing_remarks": raw_listing.get("listingRemarks"),
        "is_hot": raw_listing.get("isHot"),
        "is_new_construction": raw_listing.get("isNewConstruction"),
        "has_virtual_tour": raw_listing.get("hasVirtualTour"),
        "has_video_tour": raw_listing.get("hasVideoTour"),
        "has_3d_tour": raw_listing.get("has3DTour"),
        "new_construction_community_info": raw_listing.get("newConstructionCommunityInfo"),
        "rental_estimate": _redfin_rental_estimate_fields(raw_listing),
        "rental_estimate_error": raw_listing.get(REDFIN_RENTAL_ESTIMATE_ERROR_KEY),
    })


def _realtor_provider_metadata(raw_listing):
    return _compact_dict({
        "listing_id": raw_listing.get("listing_id"),
        "status": raw_listing.get("status"),
        "status_text": raw_listing.get("statusText"),
        "list_date": raw_listing.get("list_date"),
        "price_reduced_amount": raw_listing.get("priceReducedAmount"),
        "price_reduced_label": raw_listing.get("priceReducedLabel"),
        "photo_count": raw_listing.get("photo_count"),
        "brokerage_name": raw_listing.get("brokerageName"),
        "builder": raw_listing.get("builder"),
        "community": raw_listing.get("community"),
        "flags": raw_listing.get("flags"),
        "lead_attributes": raw_listing.get("lead_attributes"),
        "source": raw_listing.get("source"),
        "products": raw_listing.get("products"),
        "open_house_label": raw_listing.get("openHouseLabel"),
        "has_virtual_tour": raw_listing.get("hasVirtualTour"),
        "has_video_tour": raw_listing.get("hasVideoTour"),
        "has_3d_tour": raw_listing.get("has3DTour"),
    })


def _dict_or_value(value):
    if isinstance(value, dict):
        return value.get("value")
    return value


REDFIN_RENTAL_ESTIMATE_RESPONSE_KEY = "_redfin_rental_estimate_response"
REDFIN_RENTAL_ESTIMATE_ERROR_KEY = "_redfin_rental_estimate_error"


def _redfin_rental_estimate_fields(raw_listing):
    raw_listing = raw_listing or {}
    response = raw_listing.get(REDFIN_RENTAL_ESTIMATE_RESPONSE_KEY)
    if not isinstance(response, dict):
        return {}
    payload = response.get("payload") if isinstance(response.get("payload"), dict) else {}
    info = payload.get("rentalEstimateInfo") if isinstance(payload.get("rentalEstimateInfo"), dict) else {}
    if not info:
        return {}
    return _compact_dict({
        "property_id": _dict_or_value(info.get("propertyId")),
        "predicted_value": _plausible_monthly_rent(info.get("predictedValue")),
        "predicted_value_low": _plausible_monthly_rent(info.get("predictedValueLow")),
        "predicted_value_high": _plausible_monthly_rent(info.get("predictedValueHigh")),
        "should_show": _coerce_bool(info.get("shouldShow")),
        "display_level": _coerce_number(info.get("displayLevel")),
        "display_type": _coerce_number(payload.get("displayType")),
        "num_beds": _coerce_number(payload.get("numBeds")),
        "property_type_id": _coerce_number(payload.get("propertyTypeId")),
        "preview_text": payload.get("previewText"),
    })


def _collect_realtor_real_estimates(raw_listing):
    providers = {
        "collateral": "collateral_analytics",
        "collateral analytics": "collateral_analytics",
        "cotality": "cotality",
        "corelogic": "cotality",
        "quantarium": "quantarium",
    }
    estimates = {}

    def visit(value, context=""):
        if isinstance(value, dict):
            context_text = " ".join([context] + [str(value.get(key) or "") for key in ("name", "provider", "source", "label", "displayName")]).lower()
            numeric = (
                _coerce_number(value.get("estimate"))
                or _coerce_number(value.get("value"))
                or _coerce_number(value.get("price"))
                or _coerce_number(value.get("amount"))
            )
            if numeric is not None:
                for text, label in providers.items():
                    if text in context_text:
                        estimates.setdefault(label, numeric)
            for key, item in value.items():
                visit(item, f"{context} {key}")
        elif isinstance(value, list):
            for item in value:
                visit(item, context)

    visit(raw_listing)
    return estimates


class UnsupportedProviderRegionError(ValueError):
    """Raised when a provider has no usable region row for an otherwise valid ZIP."""


class ProviderBlockedError(RuntimeError):
    """
    Raised when a provider request appears blocked (captcha/interstitial/403/etc).

    This is used by the runner to apply an explicit backoff instead of
    repeatedly hammering a blocked endpoint.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        url: str = "",
        status: int | None = None,
        reason: str = "",
        reference_id: str = "",
        snippet: str = "",
    ):
        super().__init__(message)
        self.provider = provider
        self.url = url
        self.status = status
        self.reason = reason
        self.reference_id = reference_id
        self.snippet = snippet


class ListingProvider(ABC):
    source_name = "unknown"

    @abstractmethod
    def fetch_search_page(self, driver, zip_code: str, page: int):
        """Return provider-native page data for one bounded search page."""

    @abstractmethod
    def raw_listings_from_page(self, page_data) -> Iterable[dict]:
        """Extract provider-native listings from a page payload."""

    @abstractmethod
    def canonicalize_listing(self, raw_listing: dict) -> CanonicalListing:
        """Map provider-native listing data into a source-neutral listing shape."""


class ZillowListingProvider(ListingProvider):
    source_name = "zillow"

    def __init__(self, zillow_search_module, cached_query_state_data=None):
        self.zillow_search = zillow_search_module
        self.cached_query_state_data = cached_query_state_data or zillow_search_module.query_state_data
        self.current_zip_code = None
        self._user_agent = None
        self._cookie_string = None

    def prepare_session(self, driver, zip_code: str):
        zip_code = normalize_zip_code(zip_code)
        result = self.zillow_search.initialize_driver_session_for_zip_code(driver, zip_code)
        if isinstance(result, (tuple, list)) and len(result) == 2:
            self._user_agent, self._cookie_string = result[0], result[1]
        self.current_zip_code = zip_code

    def fetch_search_page(self, driver, zip_code: str, page: int):
        normalized_zip = normalize_zip_code(zip_code)
        query_state = self.cached_query_state_data.get(str(normalized_zip))
        if not query_state and str(normalized_zip).isdigit():
            query_state = self.cached_query_state_data.get(int(normalized_zip))
        if not query_state:
            raise UnsupportedProviderRegionError(f"No cached Zillow query state for ZIP {zip_code}.")
        if self.current_zip_code != normalized_zip:
            self.prepare_session(driver, normalized_zip)
        query_state_copy = deepcopy(query_state)

        fn = getattr(self.zillow_search, "scrape_listings_in_zip_code_for_page", None)
        if not callable(fn):
            raise AttributeError("zillow_search module does not define scrape_listings_in_zip_code_for_page")

        is_legacy_signature = False
        try:
            import inspect

            param_names = list(inspect.signature(fn).parameters.keys())
            is_legacy_signature = "cookie_string" in param_names and "user_agent" in param_names
        except Exception:
            is_legacy_signature = False

        if is_legacy_signature:
            if not (self._user_agent and self._cookie_string):
                result = self.zillow_search.initialize_driver_session_for_zip_code(driver, normalized_zip)
                if isinstance(result, (tuple, list)) and len(result) == 2:
                    self._user_agent, self._cookie_string = result[0], result[1]
            if not (self._user_agent and self._cookie_string):
                raise RuntimeError("Zillow legacy scraper requires user_agent/cookie_string but none were captured.")
            return fn(
                driver,
                self._user_agent,
                self._cookie_string,
                query_state_copy,
                page,
            )

        return fn(
            driver,
            query_state_copy,
            page,
        )

    def raw_listings_from_page(self, page_data) -> List[dict]:
        return (((page_data or {}).get("cat1") or {}).get("searchResults") or {}).get("listResults") or []

    def total_pages(self, page_data) -> int:
        return int((((page_data or {}).get("cat1") or {}).get("searchList") or {}).get("totalPages") or 1)

    def canonicalize_listing(self, raw_listing: dict) -> CanonicalListing:
        home_info = raw_listing.get("hdpData", {}).get("homeInfo", {})
        lat_long = raw_listing.get("latLong") or {}
        source_property_id = str(raw_listing.get("zpid") or home_info.get("zpid") or "")
        address = raw_listing.get("address") or home_info.get("streetAddress") or ""
        city, state, parsed_zip = parse_city_state_zip_from_address(address)
        city = city or home_info.get("city") or ""
        state = state or home_info.get("state") or ""
        zip_code = normalize_zip_code(home_info.get("zipcode") or raw_listing.get("addressZipcode") or parsed_zip)
        home_type = home_info.get("homeType") or raw_listing.get("statusType") or ""
        detail_property, zestimate_history = _load_zillow_detail_property(zip_code, source_property_id)
        detail_price_history = detail_property.get("priceHistory") if isinstance(detail_property.get("priceHistory"), list) else []
        detail_tax_history = detail_property.get("taxHistory") if isinstance(detail_property.get("taxHistory"), list) else []
        price = _coerce_number(raw_listing.get("unformattedPrice") or home_info.get("price") or detail_property.get("price"))
        zestimate = _coerce_number(raw_listing.get("zestimate") or home_info.get("zestimate") or detail_property.get("zestimate"))
        rent_zestimate = _coerce_number(home_info.get("rentZestimate") or detail_property.get("rentZestimate"))
        url = raw_listing.get("detailUrl") or raw_listing.get("hdpUrl") or ""
        direct_rent_listing = _direct_rent_estimate_from_listing(
            self.source_name,
            raw_listing,
            price=price,
            status=str(raw_listing.get("statusType") or raw_listing.get("statusText") or home_info.get("homeStatus") or detail_property.get("homeStatus") or ""),
            home_type=home_type,
            url=url,
        )
        remarks_rent_estimate = _remarks_monthly_rent_estimate(raw_listing)
        rent_estimate = _safe_first(rent_zestimate, direct_rent_listing, remarks_rent_estimate)
        canonical_id = canonical_property_identity(
            self.source_name,
            source_property_id=source_property_id,
            address=address,
            city=city,
            state=state,
            zip_code=zip_code,
            home_type=home_type,
        )
        return CanonicalListing(
            source_name=self.source_name,
            source_property_id=source_property_id,
            canonical_property_id=canonical_id,
            address=address,
            normalized_address=normalize_address(address),
            city=city,
            state=state,
            zip_code=zip_code,
            price=price,
            price_estimate=zestimate,
            rent_estimate=rent_estimate,
            price_estimates=_compact_dict({
                "zillow_zestimate": zestimate,
                "zillow_search_zestimate": _coerce_number(raw_listing.get("zestimate")),
                "zillow_hdp_zestimate": _coerce_number(home_info.get("zestimate")),
                "zillow_detail_zestimate": _coerce_number(detail_property.get("zestimate")),
                "zillow_zestimate_high_percent": detail_property.get("zestimateHighPercent"),
                "zillow_zestimate_low_percent": detail_property.get("zestimateLowPercent"),
            }),
            rent_estimates=_compact_dict({
                "zillow_rent_zestimate": rent_zestimate,
                "zillow_hdp_rent_zestimate": _coerce_number(home_info.get("rentZestimate")),
                "zillow_detail_rent_zestimate": _coerce_number(detail_property.get("rentZestimate")),
                "zillow_rent_listing_price": direct_rent_listing,
                "zillow_remarks_rent_estimate": remarks_rent_estimate,
                "zillow_rent_zestimate_high_percent": detail_property.get("restimateHighPercent"),
                "zillow_rent_zestimate_low_percent": detail_property.get("restimateLowPercent"),
            }),
            estimate_history=_compact_history_rows(zestimate_history),
            price_history=_compact_history_rows(detail_price_history),
            tax_history=_compact_history_rows(detail_tax_history),
            home_type=home_type,
            beds=_coerce_number(raw_listing.get("beds") or home_info.get("bedrooms") or detail_property.get("bedrooms")),
            baths=_coerce_number(raw_listing.get("baths") or home_info.get("bathrooms") or detail_property.get("bathrooms")),
            living_area=_coerce_number(raw_listing.get("area") or home_info.get("livingArea") or detail_property.get("livingArea")),
            lot_size=_lot_size_sqft(
                _safe_first(home_info.get("lotAreaValue"), detail_property.get("lotAreaValue"), detail_property.get("lotSize")),
                _safe_first(home_info.get("lotAreaUnit"), detail_property.get("lotAreaUnits")),
            ),
            year_built=_coerce_int(raw_listing.get("yearBuilt") or home_info.get("yearBuilt") or detail_property.get("yearBuilt")),
            status=str(raw_listing.get("statusType") or raw_listing.get("statusText") or home_info.get("homeStatus") or detail_property.get("homeStatus") or ""),
            latitude=_coerce_number(lat_long.get("latitude") or home_info.get("latitude") or detail_property.get("latitude")),
            longitude=_coerce_number(lat_long.get("longitude") or home_info.get("longitude") or detail_property.get("longitude")),
            provider_metadata=_zillow_provider_metadata(raw_listing, detail_property),
            url=url,
            raw=raw_listing,
        )


class RedfinListingProvider(ListingProvider):
    source_name = "redfin"

    page_size = 350

    UI_PROPERTY_TYPES = {
        1: "SINGLE_FAMILY",
        2: "CONDO",
        3: "TOWNHOUSE",
        4: "MULTI_FAMILY",
        5: "LOT",
        6: "MOBILE_MANUFACTURED",
        7: "CO_OP",
        8: "OTHER",
    }

    REDFIN_PLACE_TYPE_TO_GIS_REGION_TYPE = {
        # Redfin's location lookup returns ZIP rows as type 4, but GIS expects
        # region_type=2 for ZIP-code searches.
        "4": "2",
    }

    RENTAL_ESTIMATE_BATCH_SIZE = 5

    def __init__(self):
        self.region_cache = {}
        self.rental_estimate_cache = {}
        self.current_zip_code = None
        self.market = "florida"
        self._did_navigate = False

    def prepare_session(self, driver, zip_code: str):
        zip_code = normalize_zip_code(zip_code)
        try:
            on_redfin = "redfin.com" in (driver.current_url or "")
        except Exception:
            on_redfin = False
        # Only do a full page navigation when not already on redfin.com.
        # The GIS and region-lookup APIs only need session cookies, which persist
        # across ZIPs once established on the first visit.
        if not on_redfin:
            self._did_navigate = True
            driver.get(f"https://www.redfin.com/zipcode/{zip_code}")
        else:
            self._did_navigate = False
        self.current_zip_code = zip_code
        self.market = self._market_from_cookie(driver) or self.market
        if zip_code not in self.region_cache:
            self.region_cache[zip_code] = self._resolve_region(driver, zip_code)

    def fetch_search_page(self, driver, zip_code: str, page: int):
        zip_code = normalize_zip_code(zip_code)
        if self.current_zip_code != zip_code or zip_code not in self.region_cache:
            self.prepare_session(driver, zip_code)
        region = self.region_cache[zip_code]
        start = max(0, page - 1) * self.page_size
        params = {
            "al": "1",
            "include_nearby_homes": "true",
            "market": region.get("market") or self.market,
            "mpt": "99",
            "num_homes": str(self.page_size),
            "ord": "price-desc",
            "page_number": str(page),
            "region_id": str(region["region_id"]),
            "region_type": str(region["region_type"]),
            "sf": "1,2,3,5,6,7",
            "start": str(start),
            "status": "9",
            "uipt": "1,2,3,4,5,6,7,8",
            "v": "8",
        }
        url = "https://www.redfin.com/stingray/api/gis?" + urlencode(params)
        result = driver.execute_async_script(
            """
            const url = arguments[0];
            const done = arguments[arguments.length - 1];
            fetch(url, {
              method: "GET",
              credentials: "include",
              headers: {
                "accept": "*/*",
                "x-requested-with": "XMLHttpRequest"
              }
            }).then(async (response) => {
              const text = await response.text();
              done({
                ok: response.ok,
                status: response.status,
                url: response.url,
                text
              });
            }).catch((error) => done({
              ok: false,
              status: 0,
              error: String(error),
              url
            }));
            """,
            url,
        )
        if not result.get("ok"):
            raise RuntimeError(
                f"Redfin GIS fetch failed with status {result.get('status')}: "
                f"{result.get('error') or result.get('text', '')[:300]}"
            )
        data = self._parse_redfin_json(result.get("text") or "")
        data["_request"] = {
            "url": result.get("url") or url,
            "zip_code": zip_code,
            "page": page,
            "region": region,
        }
        return data

    def raw_listings_from_page(self, page_data):
        payload = (page_data or {}).get("payload") or {}
        original_homes = payload.get("originalHomes") or {}
        if isinstance(original_homes, dict) and isinstance(original_homes.get("homes"), list):
            return original_homes.get("homes") or []
        if isinstance(payload.get("homes"), list):
            return payload.get("homes") or []
        return []

    def total_pages(self, page_data) -> int:
        payload = (page_data or {}).get("payload") or {}
        original_homes = payload.get("originalHomes") or {}
        totals = [
            payload.get("totalHomes"),
            payload.get("totalResults"),
            original_homes.get("totalHomes") if isinstance(original_homes, dict) else None,
            original_homes.get("totalResults") if isinstance(original_homes, dict) else None,
        ]
        total = next((int(value) for value in totals if str(value or "").isdigit()), None)
        if not total:
            current_page = (((page_data or {}).get("_request") or {}).get("page") or 1)
            raw_count = len(self.raw_listings_from_page(page_data))
            return int(current_page) + 1 if raw_count >= self.page_size else int(current_page)
        return max(1, math.ceil(total / self.page_size))

    def enrich_rental_estimates(self, driver, raw_listings, limit=0, delay_seconds=0.25):
        """Attach Redfin's lightweight rental-estimate payload to raw homes.

        This intentionally runs from the active Redfin browser context so the
        request uses the same cookies/session as the search page.
        Fetches are batched via Promise.all() to avoid the latency cost of a
        serial 0.25s-per-listing loop.
        """
        attempted = 0
        succeeded = 0
        skipped = 0
        error_count = 0
        errors = []
        limit = max(0, int(limit or 0))
        delay_seconds = max(0.0, float(delay_seconds or 0.0))

        # Phase 1: collect work items (listings that still need a fetch).
        work = []
        for raw_listing in raw_listings or []:
            if not isinstance(raw_listing, dict):
                skipped += 1
                continue
            property_id = self._property_id(raw_listing)
            if not property_id:
                skipped += 1
                continue
            if REDFIN_RENTAL_ESTIMATE_RESPONSE_KEY in raw_listing:
                if _redfin_rental_estimate_fields(raw_listing).get("predicted_value") is not None:
                    succeeded += 1
                continue
            if limit and len(work) >= limit:
                break
            work.append((property_id, raw_listing))

        # Phase 2: batch-fetch uncached IDs with Promise.all().
        uncached = [pid for pid, _ in work if pid not in self.rental_estimate_cache]
        for batch_start in range(0, len(uncached), self.RENTAL_ESTIMATE_BATCH_SIZE):
            batch_ids = uncached[batch_start : batch_start + self.RENTAL_ESTIMATE_BATCH_SIZE]
            self.rental_estimate_cache.update(self._fetch_rental_estimates_batch(driver, batch_ids))
            remaining = len(uncached) - batch_start - self.RENTAL_ESTIMATE_BATCH_SIZE
            if delay_seconds > 0 and remaining > 0:
                time.sleep(delay_seconds)

        # Phase 3: assign cached results to listings.
        for property_id, raw_listing in work:
            attempted += 1
            data = self.rental_estimate_cache.get(property_id)
            if data is None:
                skipped += 1
                continue
            if isinstance(data, dict) and "payload" not in data and "error" in data:
                error_count += 1
                raw_listing[REDFIN_RENTAL_ESTIMATE_ERROR_KEY] = data["error"]
                if len(errors) < 5:
                    errors.append({"property_id": property_id, "error": data["error"]})
            else:
                raw_listing[REDFIN_RENTAL_ESTIMATE_RESPONSE_KEY] = deepcopy(data)
                if _redfin_rental_estimate_fields(raw_listing).get("predicted_value") is not None:
                    succeeded += 1

        return {
            "attempted": attempted,
            "succeeded": succeeded,
            "skipped": skipped,
            "error_count": error_count,
            "errors": errors,
        }

    def _fetch_rental_estimates_batch(self, driver, property_ids: list) -> dict:
        """Fetch rental estimates for multiple IDs concurrently via Promise.all()."""
        if not property_ids:
            return {}
        results = driver.execute_async_script(
            """
            const ids = arguments[0];
            const done = arguments[arguments.length - 1];
            Promise.all(ids.map(pid => {
                const url = "https://www.redfin.com/stingray/api/home/details/rental-estimate?"
                    + new URLSearchParams({
                        propertyId: String(pid),
                        accessLevel: "1",
                        includePrimaryPhotoUrl: "true"
                    });
                return fetch(url, {
                    method: "GET",
                    credentials: "include",
                    headers: {"accept": "*/*", "x-requested-with": "XMLHttpRequest"}
                })
                .then(async r => ({pid: String(pid), ok: r.ok, status: r.status, url: r.url, text: await r.text()}))
                .catch(e => ({pid: String(pid), ok: false, status: 0, error: String(e), url: ""}));
            })).then(rs => done(rs)).catch(e => done([{ok: false, error: String(e), pid: ""}]));
            """,
            property_ids,
        )
        out = {}
        for item in (results or []):
            if not isinstance(item, dict):
                continue
            pid = str(item.get("pid") or "")
            if not pid:
                continue
            if item.get("ok"):
                try:
                    data = self._parse_redfin_json(item.get("text") or "")
                    data["_request"] = {"url": item.get("url") or "", "property_id": pid, "kind": "rental_estimate"}
                    out[pid] = data
                except Exception as exc:
                    out[pid] = {"error": f"parse_failed: {exc}"}
            else:
                out[pid] = {"error": item.get("error") or f"http_{item.get('status')}"}
        return out

    def _fetch_rental_estimate(self, driver, property_id):
        url = (
            "https://www.redfin.com/stingray/api/home/details/rental-estimate?"
            + urlencode({
                "propertyId": str(property_id),
                "accessLevel": "1",
                "includePrimaryPhotoUrl": "true",
            })
        )
        result = driver.execute_async_script(
            """
            const url = arguments[0];
            const done = arguments[arguments.length - 1];
            fetch(url, {
              method: "GET",
              credentials: "include",
              headers: {
                "accept": "*/*",
                "x-requested-with": "XMLHttpRequest"
              }
            }).then(async (response) => {
              done({
                ok: response.ok,
                status: response.status,
                url: response.url,
                text: await response.text()
              });
            }).catch((error) => done({
              ok: false,
              status: 0,
              error: String(error),
              url
            }));
            """,
            url,
        )
        if not result.get("ok"):
            raise RuntimeError(
                f"Redfin rental estimate fetch failed with status {result.get('status')}: "
                f"{result.get('error') or result.get('text', '')[:300]}"
            )
        data = self._parse_redfin_json(result.get("text") or "")
        data["_request"] = {
            "url": result.get("url") or url,
            "property_id": str(property_id),
            "kind": "rental_estimate",
        }
        return data

    def canonicalize_listing(self, raw_listing: dict) -> CanonicalListing:
        source_property_id = str(self._property_id(raw_listing) or raw_listing.get("listingId") or "")
        street = self._value(raw_listing.get("streetLine")) or self._street_from_url(raw_listing.get("url"))
        city = raw_listing.get("city") or ""
        state = raw_listing.get("state") or ""
        zip_code = normalize_zip_code(
            self._value(raw_listing.get("postalCode")) or raw_listing.get("zip")
        )
        address_parts = [part for part in [street, city, f"{state} {zip_code}".strip()] if part]
        address = ", ".join(address_parts)
        home_type = self._home_type(raw_listing)
        canonical_id = canonical_property_identity(
            self.source_name,
            source_property_id=source_property_id,
            address=address,
            city=city,
            state=state,
            zip_code=zip_code,
            home_type=home_type,
        )
        url = raw_listing.get("url") or ""
        if url.startswith("/"):
            url = f"https://www.redfin.com{url}"
        lat_long = self._value(raw_listing.get("latLong")) or {}
        if not isinstance(lat_long, dict):
            lat_long = {}
        redfin_rental_estimate = _redfin_rental_estimate_fields(raw_listing)
        redfin_api_rent_estimate = redfin_rental_estimate.get("predicted_value")
        redfin_embedded_rent_estimate = _deep_find_first_number(raw_listing, (
            "rentalestimate",
            "rental_estimate",
            "rentestimate",
            "rent_estimate",
            "rentalearning",
            "rental_earning",
            "monthlyrent",
            "marketrent",
        ))
        redfin_value_estimate = _deep_find_first_number(raw_listing, (
            "estimate",
            "avm",
            "homevalue",
            "home_value",
        ))
        price = self._value(raw_listing.get("price"))
        direct_rent_listing = _direct_rent_estimate_from_listing(
            self.source_name,
            raw_listing,
            price=price,
            status=str(raw_listing.get("mlsStatus") or raw_listing.get("status") or ""),
            home_type=home_type,
            url=url,
        )
        remarks_rent_estimate = _remarks_monthly_rent_estimate(raw_listing)
        rent_estimate = _safe_first(redfin_api_rent_estimate, redfin_embedded_rent_estimate, direct_rent_listing, remarks_rent_estimate)
        price_history = (
            _deep_find_first_sequence(raw_listing, ("pricehistory", "salehistory", "propertyhistory"))
            or []
        )
        return CanonicalListing(
            source_name=self.source_name,
            source_property_id=source_property_id,
            canonical_property_id=canonical_id,
            address=address,
            normalized_address=normalize_address(address),
            city=city,
            state=state,
            zip_code=zip_code,
            price=price,
            price_estimate=redfin_value_estimate,
            rent_estimate=rent_estimate,
            price_estimates=_compact_dict({
                "redfin_value_estimate": redfin_value_estimate,
            }),
            rent_estimates=_compact_dict({
                "redfin_rental_estimate": redfin_api_rent_estimate,
                "redfin_rental_estimate_low": redfin_rental_estimate.get("predicted_value_low"),
                "redfin_rental_estimate_high": redfin_rental_estimate.get("predicted_value_high"),
                "redfin_rental_earnings_estimate": redfin_embedded_rent_estimate,
                "redfin_rent_listing_price": direct_rent_listing,
                "redfin_remarks_rent_estimate": remarks_rent_estimate,
            }),
            price_history=_compact_history_rows(price_history),
            home_type=home_type,
            beds=_coerce_number(raw_listing.get("beds")),
            baths=_coerce_number(raw_listing.get("baths")),
            living_area=_coerce_number(raw_listing.get("sqFt")),
            lot_size=_coerce_number(raw_listing.get("lotSize")),
            year_built=_coerce_int(raw_listing.get("yearBuilt")),
            status=str(raw_listing.get("mlsStatus") or raw_listing.get("status") or ""),
            latitude=_coerce_number(lat_long.get("latitude")),
            longitude=_coerce_number(lat_long.get("longitude")),
            provider_metadata=_redfin_provider_metadata(raw_listing),
            url=url,
            raw=raw_listing,
        )

    def _resolve_region(self, driver, zip_code: str):
        url = (
            "https://www.redfin.com/stingray/do/location-autocomplete?"
            + urlencode({"location": zip_code, "start": "0", "count": "10", "v": "2"})
        )
        result = driver.execute_async_script(
            """
            const url = arguments[0];
            const done = arguments[arguments.length - 1];
            fetch(url, {
              method: "GET",
              credentials: "include",
              headers: {
                "accept": "*/*",
                "x-requested-with": "XMLHttpRequest"
              }
            }).then(async (response) => {
              done({
                ok: response.ok,
                status: response.status,
                text: await response.text(),
                url: response.url
              });
            }).catch((error) => done({
              ok: false,
              status: 0,
              error: String(error),
              url
            }));
            """,
            url,
        )
        if not result.get("ok"):
            raise RuntimeError(
                f"Redfin location lookup failed with status {result.get('status')}: "
                f"{result.get('error') or result.get('text', '')[:300]}"
            )
        data = self._parse_redfin_json(result.get("text") or "")
        place_row = self._select_zip_place_row(data, zip_code)
        if not place_row:
            raise UnsupportedProviderRegionError(
                f"Redfin location lookup did not return a ZIP row for {zip_code}."
            )
        raw_id = str(place_row.get("id") or "")
        match = re.match(r"(?P<type>\d+)_(?P<id>\d+)$", raw_id)
        if not match:
            raise ValueError(f"Unexpected Redfin location row id for ZIP {zip_code}: {raw_id}")
        lookup_type = match.group("type")
        return {
            "region_id": match.group("id"),
            "region_type": self.REDFIN_PLACE_TYPE_TO_GIS_REGION_TYPE.get(lookup_type, lookup_type),
            "lookup_type": lookup_type,
            "market": self._market_from_cookie(driver) or self.market,
            "name": place_row.get("name"),
            "url": place_row.get("url"),
        }

    def _select_zip_place_row(self, data, zip_code: str):
        sections = (((data or {}).get("payload") or {}).get("sections") or [])
        for section in sections:
            for row in section.get("rows") or []:
                row_url = row.get("url") or ""
                if row.get("name") == zip_code and row_url.startswith("/zipcode/"):
                    return row
        for section in sections:
            for row in section.get("rows") or []:
                if str(row.get("id") or "").startswith("4_") and row.get("name") == zip_code:
                    return row
        return None

    def _parse_redfin_json(self, text: str):
        text = (text or "").strip()
        if text.startswith("{}&&"):
            text = text[4:]
        if text.startswith(")]}'"):
            text = text.split("\n", 1)[-1]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if "&&" in text[:20]:
                return json.loads(text.split("&&", 1)[1])
            raise

    def _market_from_cookie(self, driver):
        try:
            cookie_text = driver.execute_script("return document.cookie || '';") or ""
        except Exception:
            return ""
        match = re.search(r"(?:^|;)\s*RF_MARKET=([^;]+)", cookie_text)
        return match.group(1) if match else ""

    def _property_id(self, raw_listing):
        property_id = self._value((raw_listing or {}).get("propertyId"))
        if property_id:
            return str(property_id)
        url = str((raw_listing or {}).get("url") or "")
        match = re.search(r"/home/(\d+)", url)
        return match.group(1) if match else ""

    def _home_type(self, raw_listing: dict) -> str:
        ui_property_type = raw_listing.get("uiPropertyType")
        try:
            ui_property_type = int(ui_property_type)
        except (TypeError, ValueError):
            ui_property_type = None
        return self.UI_PROPERTY_TYPES.get(ui_property_type) or str(raw_listing.get("propertyType") or "")

    def _street_from_url(self, url: str) -> str:
        if not url:
            return ""
        parts = [part for part in str(url).split("/") if part]
        if len(parts) < 4:
            return ""
        candidate = parts[-3] if parts[-2] == "home" else parts[-2]
        candidate = re.sub(r"-\d{5}$", "", candidate)
        return candidate.replace("-", " ").title()

    def _value(self, maybe_value):
        if isinstance(maybe_value, dict):
            return maybe_value.get("value")
        return maybe_value


class RealtorListingProvider(ListingProvider):
    source_name = "realtor"
    page_size = 42

    def __init__(self):
        self.current_zip_code = None
        self._homepage_warmed = False

    @staticmethod
    def _extract_reference_id(text: str) -> str:
        if not text:
            return ""
        match = re.search(r"reference id is\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", str(text), re.IGNORECASE)
        return match.group(1) if match else ""

    @staticmethod
    def _looks_like_blocked_response(text: str) -> bool:
        if not text:
            return False
        head = str(text[:500] or "").lower()
        # Fast signals that the .data endpoint returned HTML / a block page.
        if "<html" in head or "<!doctype" in head:
            return True
        tokens = (
            "perimeterx",
            "px-captcha",
            "captcha",
            "press and hold",
            "press & hold",
            "verify you are human",
            "unusual traffic",
            "your request could not be processed",
            "kpsdk",
            "access denied",
            "forbidden",
            "blocked",
            "robot",
            "automated requests",
        )
        return any(token in head for token in tokens)

    @staticmethod
    def _page_text(driver, max_chars: int = 4000) -> str:
        try:
            text = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
        except Exception:
            text = ""
        return str(text)[:max(0, int(max_chars or 0))]

    def _raise_if_blocked_page(self, driver, *, url_hint: str = "", context: str = ""):
        """
        Detect Realtor.com block pages rendered in the browser.

        This is not a bypass mechanism: it only detects the block UI early so the
        runner can back off and avoid repeated blocked requests.
        """
        try:
            current_url = driver.current_url or ""
        except Exception:
            current_url = ""
        text = self._page_text(driver, max_chars=6000)
        haystack = f"{current_url}\n{text}".lower()
        # Also check raw page source: the CSS-obfuscated block variant uses color:transparent
        # animation to hide text from innerText, but the HTML source always contains the
        # telltale email address and .hp honeypot class regardless of rendering state.
        source_lower = ""
        try:
            source_lower = (driver.page_source or "")[:8000].lower()
        except Exception:
            pass
        tokens = (
            "your request could not be processed",
            "unblockrequest@realtor.com",
            "blocked ip address",
            "kpsdk",
        )
        source_blocked = (
            "unblockrequest@realtor.com" in source_lower
            or "/miscellaneous/userblocked" in source_lower
        )
        if any(token in haystack for token in tokens) or "/miscellaneous/userblocked" in haystack or source_blocked:
            reference_id = self._extract_reference_id(text) or self._extract_reference_id(source_lower)
            reason = "realtor_request_not_processed" if "your request could not be processed" in haystack else "realtor_blocked"
            if "kpsdk" in haystack:
                reason = "realtor_kpsdk_block"
            snippet = (text or "")[:600]
            message = "Realtor page appears blocked."
            if context:
                message = f"{message} context={context}"
            raise ProviderBlockedError(
                message,
                provider=self.source_name,
                url=current_url or url_hint,
                status=None,
                reason=reason,
                reference_id=reference_id,
                snippet=snippet,
            )

    # Vary the entry page each session so navigation patterns don't repeat identically.
    _WARMUP_URLS = [
        "https://www.realtor.com/",
        "https://www.realtor.com/realestateandhomes-search/Florida",
        "https://www.realtor.com/real-estate/Florida/",
        "https://www.realtor.com/local/",
        "https://www.realtor.com/research/florida-housing-market/",
    ]

    def _warm_homepage(self, driver):
        if self._homepage_warmed:
            return
        url = random.choice(self._WARMUP_URLS)
        try:
            driver.get(url)
        except Exception:
            return
        self._homepage_warmed = True
        try:
            time.sleep(random.uniform(0.8, 2.0))
        except Exception:
            pass

    def prepare_session(self, driver, zip_code: str):
        zip_code = normalize_zip_code(zip_code)
        self._warm_homepage(driver)
        driver.get(f"https://www.realtor.com/realestateandhomes-search/{zip_code}")
        self._raise_if_blocked_page(driver, url_hint=f"https://www.realtor.com/realestateandhomes-search/{zip_code}", context="prepare_session")
        self.current_zip_code = zip_code

    def fetch_search_page(self, driver, zip_code: str, page: int):
        zip_code = normalize_zip_code(zip_code)
        if self.current_zip_code != zip_code:
            self.prepare_session(driver, zip_code)
        if page <= 1:
            path = f"/realestateandhomes-search/{zip_code}.data?_routes=srp"
        else:
            path = f"/realestateandhomes-search/{zip_code}/pg-{page}.data?_routes=srp"
        url = f"https://www.realtor.com{path}"
        result = driver.execute_async_script(
            """
            const url = arguments[0];
            const done = arguments[arguments.length - 1];
            fetch(url, {
              method: "GET",
              credentials: "include",
            }).then(async (response) => {
              done({
                ok: response.ok,
                status: response.status,
                url: response.url,
                text: await response.text()
              });
            }).catch((error) => done({
              ok: false,
              status: 0,
              error: String(error),
              url
            }));
            """,
            url,
        )
        status = result.get("status")
        body_text = result.get("text") or ""
        if (not result.get("ok")) or (status in {401, 403, 407, 409, 429, 451, 500, 502, 503, 520, 521, 522}):
            snippet = (result.get("error") or body_text or "")[:600]
            raise ProviderBlockedError(
                f"Realtor data fetch failed with status {status}",
                provider=self.source_name,
                url=result.get("url") or url,
                status=int(status) if status not in (None, "") else None,
                reason="fetch_status_not_ok",
                snippet=snippet,
            )
        if self._looks_like_blocked_response(body_text):
            lower_text = body_text[:5000].lower()
            reference_id = self._extract_reference_id(body_text)
            reason = "blocked_body"
            if "kpsdk" in lower_text:
                reason = "realtor_kpsdk_block"
            elif "your request could not be processed" in lower_text:
                reason = "realtor_request_not_processed"
            raise ProviderBlockedError(
                "Realtor data response looks like a block page (HTML/captcha).",
                provider=self.source_name,
                url=result.get("url") or url,
                status=int(status) if status not in (None, "") else None,
                reason=reason,
                reference_id=reference_id,
                snippet=body_text[:600],
            )
        try:
            decoded = self._parse_single_fetch(body_text)
        except Exception as exc:
            if self._looks_like_blocked_response(body_text):
                raise ProviderBlockedError(
                    f"Realtor data response could not be decoded ({type(exc).__name__}).",
                    provider=self.source_name,
                    url=result.get("url") or url,
                    status=int(status) if status not in (None, "") else None,
                    reason="decode_failed_blocked_body",
                    snippet=body_text[:600],
                ) from exc
            raise
        decoded["_request"] = {
            "url": result.get("url") or url,
            "zip_code": zip_code,
            "page": page,
        }
        return decoded

    def raw_listings_from_page(self, page_data):
        search = self._search_payload(page_data)
        properties = search.get("properties") if isinstance(search, dict) else None
        return properties if isinstance(properties, list) else []

    def total_pages(self, page_data) -> int:
        search = self._search_payload(page_data)
        total = search.get("total") if isinstance(search, dict) else None
        current_page = (((page_data or {}).get("_request") or {}).get("page") or 1)
        raw_count = len(self.raw_listings_from_page(page_data))
        if isinstance(total, (int, float)):
            return max(1, math.ceil(total / self.page_size)) if total > 0 else 1
        return int(current_page) + 1 if raw_count else int(current_page)

    def canonicalize_listing(self, raw_listing: dict) -> CanonicalListing:
        source_property_id = str(raw_listing.get("property_id") or "")
        location = raw_listing.get("location") or {}
        address_info = location.get("address") or {}
        street = address_info.get("line") or ""
        city = address_info.get("city") or ""
        state = address_info.get("state_code") or ""
        zip_code = normalize_zip_code(address_info.get("postal_code"))
        address_parts = [part for part in [street, city, f"{state} {zip_code}".strip()] if part]
        address = ", ".join(address_parts)
        description = raw_listing.get("description") or {}
        home_type = description.get("type") or raw_listing.get("statusText") or ""
        canonical_id = canonical_property_identity(
            self.source_name,
            source_property_id=source_property_id,
            address=address,
            city=city,
            state=state,
            zip_code=zip_code,
            home_type=home_type,
        )
        url = raw_listing.get("href") or ""
        if not url and raw_listing.get("ldpSlug"):
            url = f"https://www.realtor.com/realestateandhomes-detail/{raw_listing.get('ldpSlug')}"
        elif url.startswith("/"):
            url = f"https://www.realtor.com{url}"
        realtor_real_estimates = _collect_realtor_real_estimates(raw_listing)
        realtor_price_estimate = next(iter(realtor_real_estimates.values()), None)
        realtor_rent_estimate = _deep_find_first_number(raw_listing, (
            "rentestimate",
            "rent_estimate",
            "rentalestimate",
            "rental_estimate",
            "marketrent",
            "monthlyrent",
        ))
        price = _coerce_number(raw_listing.get("list_price"))
        direct_rent_listing = _direct_rent_estimate_from_listing(
            self.source_name,
            raw_listing,
            price=price,
            status=str(raw_listing.get("status") or raw_listing.get("statusText") or ""),
            home_type=home_type,
            url=url,
        )
        remarks_rent_estimate = _remarks_monthly_rent_estimate(raw_listing)
        rent_estimate = _safe_first(realtor_rent_estimate, direct_rent_listing, remarks_rent_estimate)
        price_history = _deep_find_first_sequence(raw_listing, ("pricehistory", "salehistory", "propertyhistory"))
        return CanonicalListing(
            source_name=self.source_name,
            source_property_id=source_property_id,
            canonical_property_id=canonical_id,
            address=address,
            normalized_address=normalize_address(address),
            city=city,
            state=state,
            zip_code=zip_code,
            price=price,
            price_estimate=realtor_price_estimate,
            rent_estimate=rent_estimate,
            price_estimates=realtor_real_estimates,
            rent_estimates=_compact_dict({
                "realtor_rent_estimate": realtor_rent_estimate,
                "realtor_rent_listing_price": direct_rent_listing,
                "realtor_remarks_rent_estimate": remarks_rent_estimate,
            }),
            price_history=_compact_history_rows(price_history),
            home_type=home_type,
            beds=_coerce_number(description.get("beds")),
            baths=_coerce_number(description.get("baths") or description.get("baths_consolidated")),
            living_area=_coerce_number(description.get("sqft")),
            lot_size=_coerce_number(description.get("lot_sqft")),
            year_built=_coerce_int(description.get("year_built")),
            status=str(raw_listing.get("status") or raw_listing.get("statusText") or ""),
            latitude=_coerce_number(raw_listing.get("lat") or ((address_info.get("coordinate") or {}).get("lat") if isinstance(address_info.get("coordinate"), dict) else None)),
            longitude=_coerce_number(raw_listing.get("lng") or ((address_info.get("coordinate") or {}).get("lon") if isinstance(address_info.get("coordinate"), dict) else None)),
            provider_metadata=_realtor_provider_metadata(raw_listing),
            url=url,
            raw=raw_listing,
        )

    def _search_payload(self, page_data):
        return (((page_data or {}).get("srp") or {}).get("data") or {}).get("search") or {}

    def _parse_single_fetch(self, text: str):
        lines = [line for line in (text or "").splitlines() if line.strip()]
        if not lines:
            raise ValueError("Empty Realtor single-fetch response.")

        values = {}
        root = json.loads(lines[0])
        for index, value in enumerate(root):
            values[index] = value

        for line in lines[1:]:
            match = re.match(r"^P(?P<id>\d+):(?P<payload>.*)$", line)
            if not match:
                continue
            promise_id = int(match.group("id"))
            parsed = json.loads(match.group("payload"))
            if isinstance(parsed, list):
                first_value = parsed[0]
                rest = parsed[1:]
                if rest:
                    start_id = next((ref for ref in self._iter_encoded_refs(first_value) if ref not in values), None)
                    if start_id is None:
                        raise ValueError(f"Could not infer Realtor chunk start id for P{promise_id}.")
                    for offset, value in enumerate(rest):
                        values[start_id + offset] = value
                values[promise_id] = first_value
            else:
                values[promise_id] = parsed

        return self._decode_realtor_ref(0, values, {})

    def _decode_realtor_ref(self, ref_id, values, cache):
        if ref_id < 0:
            return None
        if ref_id in cache:
            return cache[ref_id]
        cache[ref_id] = None
        cache[ref_id] = self._decode_realtor_value(values.get(ref_id), values, cache)
        return cache[ref_id]

    def _decode_realtor_value(self, value, values, cache):
        if isinstance(value, dict):
            decoded = {}
            for key, ref in value.items():
                match = re.fullmatch(r"_(\d+)", key)
                decoded_key = self._decode_realtor_ref(int(match.group(1)), values, cache) if match else key
                decoded_value = self._decode_realtor_ref(ref, values, cache) if isinstance(ref, int) else self._decode_realtor_value(ref, values, cache)
                decoded[decoded_key] = decoded_value
            return decoded
        if isinstance(value, list):
            if len(value) == 2 and value[0] == "P" and isinstance(value[1], int):
                return self._decode_realtor_ref(value[1], values, cache)
            return [
                self._decode_realtor_ref(item, values, cache) if isinstance(item, int) else self._decode_realtor_value(item, values, cache)
                for item in value
            ]
        return value

    def _iter_encoded_refs(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                match = re.fullmatch(r"_(\d+)", key)
                if match:
                    yield int(match.group(1))
                yield from self._iter_encoded_refs(item)
        elif isinstance(value, list):
            if len(value) == 2 and value[0] == "P" and isinstance(value[1], int):
                yield value[1]
            else:
                for item in value:
                    if isinstance(item, int) and item >= 0:
                        yield item
                    else:
                        yield from self._iter_encoded_refs(item)
        elif isinstance(value, int) and value >= 0:
            yield value
