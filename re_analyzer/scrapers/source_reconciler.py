import argparse
import csv
import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path

from re_analyzer.scrapers.scraper_runner import _load_provider, _load_provider_zip_metadata
from re_analyzer.scrapers import scraping_utility
from re_analyzer.scrapers.page_diagnostics import save_page_diagnostics
from re_analyzer.scrapers.property_identity import normalize_address
from re_analyzer.scrapers.provider_adapters import (
    _direct_rent_estimate_from_listing,
    _estimate_key_audit,
    _load_zillow_detail_property,
    _redfin_rental_estimate_fields,
    _redfin_provider_metadata,
    _realtor_provider_metadata,
    _remarks_monthly_rent_estimate,
    _zillow_provider_metadata,
)
from re_analyzer.utility.utility import (
    DATA_PATH,
    SEARCH_LISTINGS_DATA_PATH,
    ensure_directory_exists,
    load_json,
    save_json,
)


DEFAULT_PROVIDERS = ("zillow", "redfin", "realtor")
PROVIDER_URL_BASES = {
    "zillow": "https://www.zillow.com",
    "redfin": "https://www.redfin.com",
    "realtor": "https://www.realtor.com",
}
STREET_SUFFIXES = {
    "street": "st",
    "st": "st",
    "avenue": "ave",
    "ave": "ave",
    "boulevard": "blvd",
    "blvd": "blvd",
    "road": "rd",
    "rd": "rd",
    "drive": "dr",
    "dr": "dr",
    "lane": "ln",
    "ln": "ln",
    "court": "ct",
    "ct": "ct",
    "circle": "cir",
    "cir": "cir",
    "place": "pl",
    "pl": "pl",
    "terrace": "ter",
    "ter": "ter",
    "trail": "trl",
    "trl": "trl",
    "highway": "hwy",
    "hwy": "hwy",
    "parkway": "pkwy",
    "pkwy": "pkwy",
    "square": "sq",
    "sq": "sq",
    "beach": "bch",
    "bch": "bch",
    "key": "ky",
    "ky": "ky",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northeast": "ne",
    "northwest": "nw",
    "southeast": "se",
    "southwest": "sw",
    "n": "n",
    "s": "s",
    "e": "e",
    "w": "w",
    "ne": "ne",
    "nw": "nw",
    "se": "se",
    "sw": "sw",
}
DIRECTION_TOKENS = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}
ORDINAL_RE = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b")
TRAILING_ZIP_RE = re.compile(r"(?:\s+|-)(\d{5})(?:\s+|$)")
SPACE_RE = re.compile(r"\s+")


@dataclass
class ReconciledRecord:
    provider: str
    source_property_id: str
    address: str
    street_key: str
    base_street_key: str
    unit_key: str
    match_keys: list[str]
    city: str
    state: str
    zip_code: str
    price: float | int | None
    price_estimate: float | int | None
    home_type: str
    rent_estimate: float | int | None
    price_estimates: dict
    rent_estimates: dict
    estimate_history_count: int
    price_history_count: int
    tax_history_count: int
    normalized_home_type: str
    beds: float | int | None
    baths: float | int | None
    living_area: float | int | None
    lot_size: float | int | None
    year_built: int | None
    status: str
    latitude: float | None
    longitude: float | None
    provider_metadata: dict
    estimate_key_audit: dict
    tags: list[str]
    url: str


def _latest_file(directory: Path, pattern: str):
    paths = sorted(directory.glob(pattern))
    return str(paths[-1]) if paths else ""


def _canonical_path(provider_name, zip_code):
    metadata = _load_provider_zip_metadata(provider_name, zip_code)
    saved_paths = metadata.get("saved_paths") or {}
    path = saved_paths.get("canonical_path")
    if path and os.path.exists(path):
        return path
    return _latest_file(Path(DATA_PATH) / "Fetched" / provider_name / str(zip_code), "canonical_listings_*.json")


def _zillow_raw_path(zip_code):
    return _latest_file(Path(SEARCH_LISTINGS_DATA_PATH) / str(zip_code), "listings_*.json")


def _load_canonical_records(provider_name, zip_code):
    path = _canonical_path(provider_name, zip_code)
    if path:
        records = load_json(path)
        return records if isinstance(records, list) else []

    if provider_name == "zillow":
        raw_path = _zillow_raw_path(zip_code)
        if not raw_path:
            return []
        provider = _load_provider("zillow")
        raw_records = load_json(raw_path)
        return [asdict(provider.canonicalize_listing(raw)) for raw in raw_records if isinstance(raw, dict)]
    return []


def _street_from_address(address):
    return str(address or "").split(",", 1)[0].strip()


def _address_like_from_url(provider_name, url):
    url = str(url or "")
    if not url:
        return ""
    parts = [part for part in re.split(r"/+", url) if part]
    if provider_name == "redfin":
        for index, part in enumerate(parts):
            if part in {"FL", "Florida"} and index + 2 < len(parts):
                return TRAILING_ZIP_RE.sub(" ", parts[index + 2]).replace("-", " ")
    if provider_name == "zillow":
        if "homedetails" in parts:
            candidate = parts[parts.index("homedetails") + 1]
            candidate = re.sub(r"/.*$", "", candidate)
            candidate = re.sub(r"-\d+_zpid.*$", "", candidate)
            candidate = re.sub(r"-FL-\d{5}.*$", "", candidate)
            candidate = TRAILING_ZIP_RE.sub(" ", candidate)
            return candidate.replace("-", " ")
    if provider_name == "realtor":
        try:
            detail = next(part for part in parts if part.startswith("realestateandhomes-detail"))
            detail_index = parts.index(detail)
            slug = parts[detail_index + 1]
        except (StopIteration, ValueError, IndexError):
            return ""
        tokens = [token for token in slug.split("_") if token]
        state_index = next((index for index, token in enumerate(tokens) if token.upper() == "FL"), None)
        if state_index is None or state_index < 2:
            return ""
        street_token = tokens[state_index - 2]
        return TRAILING_ZIP_RE.sub(" ", street_token).replace("-", " ")
    return ""


def _street_key(address):
    street = normalize_address(_street_from_address(address))
    street = ORDINAL_RE.sub(r"\1", street)
    words = []
    for word in street.split():
        if word in {"nd", "rd", "th"} and words and words[-1].isdigit():
            continue
        normalized_word = STREET_SUFFIXES.get(word, word)
        if words and words[-1] == normalized_word and normalized_word in set(STREET_SUFFIXES.values()):
            continue
        words.append(normalized_word)
    return SPACE_RE.sub(" ", " ".join(words)).strip()


def _base_street_key(street_key):
    text = re.sub(r"\b(?:unit|apt|apartment|suite|ste|slip|lot)\s+[a-z0-9-]+\b", "", street_key or "")
    text = re.sub(r"#[a-z0-9-]+\b", "", text)
    text = re.sub(r"\b(?:and|&)\s+\d+\b", "", text)
    return SPACE_RE.sub(" ", text).strip()


def _directionless_base_key(street_key):
    words = [word for word in (street_key or "").split() if word not in DIRECTION_TOKENS]
    return SPACE_RE.sub(" ", " ".join(words)).strip()


def _direction_tokens(street_key):
    return {word for word in (street_key or "").split() if word in DIRECTION_TOKENS}


def _street_number(street_key):
    match = re.match(r"^(\d+)\b", street_key or "")
    return match.group(1) if match else ""


def _unit_key(street_key):
    match = re.search(r"(?:\bunit\s+|\bapt\s+|\bapartment\s+|\bslip\s+|#)([a-z0-9-]+)\b", street_key or "")
    return match.group(1) if match else ""


def _match_keys(provider_name, address, url, zip_code):
    keys = []
    for candidate in [address, _address_like_from_url(provider_name, url)]:
        street_key = _street_key(candidate)
        base_key = _base_street_key(street_key)
        unit_key = _unit_key(street_key)
        if base_key:
            keys.append(f"{zip_code}|{base_key}|{unit_key}")
            if not unit_key:
                keys.append(f"{zip_code}|{base_key}|")
    return list(dict.fromkeys(keys))


def _dict_value(value):
    if isinstance(value, dict):
        return value.get("value")
    return value


def _value_present(value):
    return value not in (None, "", [], {})


def _number_value(*values):
    for value in values:
        value = _dict_value(value)
        if isinstance(value, bool) or value in {"", None}:
            continue
        if isinstance(value, (int, float)):
            return value
        text = re.sub(r"[^0-9.]", "", str(value))
        if not text:
            continue
        try:
            return float(text) if "." in text else int(text)
        except ValueError:
            continue
    return None


def _lot_size_sqft(value, unit=""):
    number = _number_value(value)
    if number is None:
        return None
    unit_text = str(unit or "").strip().lower()
    if unit_text in {"acre", "acres", "ac"}:
        return round(float(number) * 43560, 2)
    return number


def _dict_or_empty(value):
    return value if isinstance(value, dict) else {}


def _list_count(value):
    return len(value) if isinstance(value, list) else 0


def _deep_find_first_number(data, key_fragments):
    key_fragments = tuple(fragment.lower() for fragment in key_fragments)
    if isinstance(data, dict):
        for key, value in data.items():
            key_text = str(key).lower()
            if any(fragment in key_text for fragment in key_fragments):
                number = _number_value(value)
                if number is not None:
                    return number
                if isinstance(value, dict):
                    for nested_key in ("value", "amount", "estimate", "price", "current"):
                        number = _number_value(value.get(nested_key))
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


def _collect_realtor_real_estimates(raw):
    provider_aliases = {
        "collateral analytics": "collateral_analytics",
        "collateral": "collateral_analytics",
        "cotality": "cotality",
        "corelogic": "cotality",
        "quantarium": "quantarium",
    }
    estimates = {}

    def visit(value, context=""):
        if isinstance(value, dict):
            context_text = " ".join([context] + [str(value.get(key) or "") for key in ("name", "provider", "source", "label", "displayName")]).lower()
            numeric = _number_value(value.get("estimate"), value.get("value"), value.get("price"), value.get("amount"))
            if numeric is not None:
                for text, label in provider_aliases.items():
                    if text in context_text:
                        estimates.setdefault(label, numeric)
            for key, item in value.items():
                visit(item, f"{context} {key}")
        elif isinstance(value, list):
            for item in value:
                visit(item, context)

    visit(raw)
    return estimates


def _normalized_home_type(value):
    text = normalize_address(str(value or ""))
    if not text:
        return ""
    if any(token in text for token in ["single", "house"]):
        return "single_family"
    if any(token in text for token in ["townhome", "townhouse", "townhomes"]):
        return "townhouse"
    if any(token in text for token in ["condo", "condomini"]):
        return "condo"
    if any(token in text for token in ["multi", "duplex", "triplex"]):
        return "multi_family"
    if any(token in text for token in ["mobile", "manufactured"]):
        return "mobile_manufactured"
    if any(token in text for token in ["lot", "land", "acre"]):
        return "land"
    if "co op" in text or "coop" in text:
        return "co_op"
    return text.replace(" ", "_")


def _tags_for_record(provider_name, item, address, home_type):
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    tags = set()
    address_text = normalize_address(address)
    url_text = normalize_address(item.get("url") or "")
    raw_text = normalize_address(json.dumps(raw)[:3000] if raw else "")
    home_type_text = normalize_address(home_type)

    if not address_text or address_text in {"0", "undisclosed address"} or "undisclosed" in address_text:
        tags.add("undisclosed_or_missing_address")
    if re.search(r"\b(?:plan|community|collection|available soon)\b", address_text) or "plan" in url_text:
        tags.add("new_construction_plan")
    if provider_name == "realtor":
        flags = raw.get("flags") if isinstance(raw.get("flags"), dict) else {}
        if flags.get("is_new_construction") or flags.get("is_plan") or raw.get("isNewHomeLead"):
            tags.add("new_construction_plan")
        if raw.get("hasToBeBuiltBadge") or raw.get("status") in {"ready_to_build", "to_be_built"}:
            tags.add("to_be_built")
    if re.search(r"\b(?:&| and )\b", address_text) or re.search(r"\bsw \d+(?:st|nd|rd|th)?\s+(?:ave|st|rd)\b", address_text):
        tags.add("intersection_or_generic_address")
    if _normalized_home_type(home_type_text) == "land" or any(token in raw_text for token in ["acre lot", "land for sale"]):
        tags.add("land_or_lot")
    return sorted(tags)


def _metadata_from_item(provider_name, item):
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    canonical_metadata = {
        field: item.get(field)
        for field in (
            "price_estimate",
            "rent_estimate",
            "price_estimates",
            "rent_estimates",
            "estimate_history",
            "price_history",
            "tax_history",
            "beds",
            "baths",
            "living_area",
            "lot_size",
            "year_built",
            "status",
            "latitude",
            "longitude",
            "provider_metadata",
        )
        if _value_present(item.get(field))
    }
    raw_metadata = {}
    if provider_name == "zillow":
        home_info = ((raw.get("hdpData") or {}).get("homeInfo") or {})
        lat_long = raw.get("latLong") or {}
        source_property_id = str(item.get("source_property_id") or raw.get("zpid") or home_info.get("zpid") or "")
        detail_property, zestimate_history = _load_zillow_detail_property(item.get("zip_code"), source_property_id)
        price_estimate = _number_value(raw.get("zestimate"), home_info.get("zestimate"))
        rent_estimate = _number_value(home_info.get("rentZestimate"))
        raw_metadata = {
            "price_estimate": _number_value(price_estimate, detail_property.get("zestimate")),
            "rent_estimate": _number_value(rent_estimate, detail_property.get("rentZestimate")),
            "price_estimates": {
                key: value for key, value in {
                    "zillow_zestimate": _number_value(price_estimate, detail_property.get("zestimate")),
                    "zillow_search_zestimate": _number_value(raw.get("zestimate")),
                    "zillow_hdp_zestimate": _number_value(home_info.get("zestimate")),
                    "zillow_detail_zestimate": _number_value(detail_property.get("zestimate")),
                }.items() if value is not None
            },
            "rent_estimates": {
                key: value for key, value in {
                    "zillow_rent_zestimate": _number_value(rent_estimate, detail_property.get("rentZestimate")),
                    "zillow_hdp_rent_zestimate": _number_value(home_info.get("rentZestimate")),
                    "zillow_detail_rent_zestimate": _number_value(detail_property.get("rentZestimate")),
                }.items() if value is not None
            },
            "estimate_history": zestimate_history or [],
            "price_history": detail_property.get("priceHistory") if isinstance(detail_property.get("priceHistory"), list) else [],
            "tax_history": detail_property.get("taxHistory") if isinstance(detail_property.get("taxHistory"), list) else [],
            "beds": _number_value(raw.get("beds"), home_info.get("bedrooms"), detail_property.get("bedrooms")),
            "baths": _number_value(raw.get("baths"), home_info.get("bathrooms"), detail_property.get("bathrooms")),
            "living_area": _number_value(raw.get("area"), home_info.get("livingArea"), detail_property.get("livingArea")),
            "lot_size": _lot_size_sqft(
                home_info.get("lotAreaValue") or detail_property.get("lotAreaValue") or detail_property.get("lotSize"),
                home_info.get("lotAreaUnit") or detail_property.get("lotAreaUnits"),
            ),
            "year_built": _number_value(raw.get("yearBuilt"), home_info.get("yearBuilt"), detail_property.get("yearBuilt")),
            "status": str(raw.get("statusType") or raw.get("statusText") or home_info.get("homeStatus") or detail_property.get("homeStatus") or ""),
            "latitude": _number_value(lat_long.get("latitude"), home_info.get("latitude"), detail_property.get("latitude")),
            "longitude": _number_value(lat_long.get("longitude"), home_info.get("longitude"), detail_property.get("longitude")),
            "provider_metadata": _zillow_provider_metadata(raw, detail_property),
        }
    elif provider_name == "redfin":
        lat_long = (_dict_value(raw.get("latLong")) or {}) if isinstance(_dict_value(raw.get("latLong")), dict) else {}
        rental_estimate_fields = _redfin_rental_estimate_fields(raw)
        api_rent_estimate = rental_estimate_fields.get("predicted_value")
        embedded_rent_estimate = _deep_find_first_number(raw, (
            "rentalestimate", "rental_estimate", "rentestimate", "rent_estimate",
            "rentalearning", "rental_earning", "monthlyrent", "marketrent",
        ))
        rent_estimate = _number_value(api_rent_estimate, embedded_rent_estimate)
        price_estimate = _deep_find_first_number(raw, ("estimate", "avm", "homevalue", "home_value"))
        raw_metadata = {
            "price_estimate": price_estimate,
            "rent_estimate": rent_estimate,
            "price_estimates": {"redfin_value_estimate": price_estimate} if price_estimate is not None else {},
            "rent_estimates": {
                key: value for key, value in {
                    "redfin_rental_estimate": api_rent_estimate,
                    "redfin_rental_estimate_low": rental_estimate_fields.get("predicted_value_low"),
                    "redfin_rental_estimate_high": rental_estimate_fields.get("predicted_value_high"),
                    "redfin_rental_earnings_estimate": embedded_rent_estimate,
                }.items() if value is not None
            },
            "estimate_history": [],
            "price_history": raw.get("priceHistory") or raw.get("saleHistory") or [],
            "tax_history": [],
            "beds": _number_value(raw.get("beds")),
            "baths": _number_value(raw.get("baths")),
            "living_area": _number_value(raw.get("sqFt")),
            "lot_size": _number_value(raw.get("lotSize")),
            "year_built": _number_value(raw.get("yearBuilt")),
            "status": str(raw.get("mlsStatus") or raw.get("status") or ""),
            "latitude": _number_value(lat_long.get("latitude")),
            "longitude": _number_value(lat_long.get("longitude")),
            "provider_metadata": _redfin_provider_metadata(raw),
        }
    elif provider_name == "realtor":
        description = raw.get("description") if isinstance(raw.get("description"), dict) else {}
        address_info = (((raw.get("location") or {}).get("address") or {}) if isinstance(raw.get("location"), dict) else {})
        coordinate = address_info.get("coordinate") if isinstance(address_info.get("coordinate"), dict) else {}
        real_estimates = _collect_realtor_real_estimates(raw)
        rent_estimate = _deep_find_first_number(raw, (
            "rentestimate", "rent_estimate", "rentalestimate", "rental_estimate",
            "monthlyrent", "marketrent",
        ))
        raw_metadata = {
            "price_estimate": next(iter(real_estimates.values()), None),
            "rent_estimate": rent_estimate,
            "price_estimates": real_estimates,
            "rent_estimates": {"realtor_rent_estimate": rent_estimate} if rent_estimate is not None else {},
            "estimate_history": [],
            "price_history": raw.get("priceHistory") or raw.get("saleHistory") or [],
            "tax_history": [],
            "beds": _number_value(description.get("beds")),
            "baths": _number_value(description.get("baths"), description.get("baths_consolidated")),
            "living_area": _number_value(description.get("sqft")),
            "lot_size": _number_value(description.get("lot_sqft")),
            "year_built": _number_value(description.get("year_built")),
            "status": str(raw.get("status") or raw.get("statusText") or ""),
            "latitude": _number_value(raw.get("lat"), coordinate.get("lat")),
            "longitude": _number_value(raw.get("lng"), coordinate.get("lon")),
            "provider_metadata": _realtor_provider_metadata(raw),
        }
    metadata = {
        field: canonical_metadata.get(field) if field in canonical_metadata else raw_metadata.get(field)
        for field in (
            "price_estimate",
            "rent_estimate",
            "price_estimates",
            "rent_estimates",
            "estimate_history",
            "price_history",
            "tax_history",
            "beds",
            "baths",
            "living_area",
            "lot_size",
            "year_built",
            "status",
            "latitude",
            "longitude",
            "provider_metadata",
        )
    }
    price = _number_value(item.get("price"), raw.get("price"), raw.get("list_price"), raw.get("unformattedPrice"))
    direct_rent_listing = _direct_rent_estimate_from_listing(
        provider_name,
        raw,
        price=price,
        status=metadata.get("status") or "",
        home_type=item.get("home_type") or "",
        url=item.get("url") or raw.get("url") or raw.get("href") or "",
    )
    remarks_rent_estimate = _remarks_monthly_rent_estimate(raw)
    rent_estimates = dict(_dict_or_empty(metadata.get("rent_estimates")))
    if direct_rent_listing is not None:
        rent_estimates.setdefault(f"{provider_name}_rent_listing_price", direct_rent_listing)
    if remarks_rent_estimate is not None:
        rent_estimates.setdefault(f"{provider_name}_remarks_rent_estimate", remarks_rent_estimate)
    if rent_estimates:
        metadata["rent_estimates"] = rent_estimates
    if not _has_metadata_value("rent_estimate", metadata.get("rent_estimate")):
        metadata["rent_estimate"] = direct_rent_listing if direct_rent_listing is not None else remarks_rent_estimate
    return metadata


def _record_from_dict(provider_name, item):
    street_key = _street_key(item.get("address") or "")
    url = str(item.get("url") or "")
    zip_code = str(item.get("zip_code") or "")
    metadata = _metadata_from_item(provider_name, item)
    home_type = str(item.get("home_type") or "")
    return ReconciledRecord(
        provider=provider_name,
        source_property_id=str(item.get("source_property_id") or ""),
        address=str(item.get("address") or ""),
        street_key=street_key,
        base_street_key=_base_street_key(street_key),
        unit_key=_unit_key(street_key),
        match_keys=_match_keys(provider_name, item.get("address") or "", url, zip_code),
        city=str(item.get("city") or ""),
        state=str(item.get("state") or ""),
        zip_code=zip_code,
        price=item.get("price"),
        price_estimate=metadata.get("price_estimate"),
        home_type=home_type,
        rent_estimate=metadata.get("rent_estimate"),
        price_estimates=_dict_or_empty(metadata.get("price_estimates")),
        rent_estimates=_dict_or_empty(metadata.get("rent_estimates")),
        estimate_history_count=_list_count(metadata.get("estimate_history")),
        price_history_count=_list_count(metadata.get("price_history")),
        tax_history_count=_list_count(metadata.get("tax_history")),
        normalized_home_type=_normalized_home_type(home_type),
        beds=metadata.get("beds"),
        baths=metadata.get("baths"),
        living_area=metadata.get("living_area"),
        lot_size=metadata.get("lot_size"),
        year_built=metadata.get("year_built"),
        status=metadata.get("status") or "",
        latitude=metadata.get("latitude"),
        longitude=metadata.get("longitude"),
        provider_metadata=_dict_or_empty(metadata.get("provider_metadata")),
        estimate_key_audit=_estimate_key_audit(item.get("raw") if isinstance(item.get("raw"), dict) else {}),
        tags=_tags_for_record(provider_name, item, item.get("address") or "", home_type),
        url=url,
    )


def _dedupe_provider_records(records):
    deduped = {}
    for record in records:
        key = (
            record.provider,
            record.source_property_id or record.street_key,
            record.zip_code,
            record.price,
        )
        deduped.setdefault(key, record)
    return list(deduped.values())


def _cluster_records(records, fuzzy_threshold=0.93):
    clusters = []
    key_index = {}
    fuzzy_blocks = defaultdict(list)

    for record in records:
        matched_index = None
        for match_key in record.match_keys:
            if match_key in key_index:
                matched_index = key_index[match_key]
                break
        if matched_index is not None:
            clusters[matched_index].append(record)
            for match_key in record.match_keys:
                key_index.setdefault(match_key, matched_index)
            continue

        block_key = (record.zip_code, _street_number(record.base_street_key), record.unit_key)
        if record.zip_code and _street_number(record.street_key):
            for cluster_index in fuzzy_blocks.get(block_key, []):
                representative = clusters[cluster_index][0]
                if representative.unit_key != record.unit_key:
                    continue
                ratio = SequenceMatcher(None, representative.base_street_key, record.base_street_key).ratio()
                if ratio >= fuzzy_threshold:
                    matched_index = cluster_index
                    break

        if matched_index is None:
            matched_index = len(clusters)
            clusters.append([])
            fuzzy_blocks[block_key].append(matched_index)
        clusters[matched_index].append(record)
        for match_key in record.match_keys:
            key_index.setdefault(match_key, matched_index)
    return clusters


def _provider_counts(records):
    return dict(Counter(record.provider for record in records))


METADATA_FIELDS = (
    "home_type",
    "rent_estimate",
    "price_estimate",
    "rent_estimates",
    "price_estimates",
    "estimate_history_count",
    "price_history_count",
    "tax_history_count",
    "beds",
    "baths",
    "living_area",
    "lot_size",
    "year_built",
    "status",
    "latitude",
    "longitude",
    "provider_metadata",
)
PROVIDER_ONLY_CLASS_LABELS = (
    "new_construction_plan",
    "undisclosed_or_missing_address",
    "intersection_or_generic_address",
    "land_or_lot",
    "weak_address",
    "true_source_only_candidate",
)


def _has_value(value):
    return _value_present(value)


def _has_metadata_value(field, value):
    if field.endswith("_count"):
        return isinstance(value, (int, float)) and value > 0
    return _has_value(value)


def _best_metadata(records):
    best = {}
    provider_values = defaultdict(dict)
    for field in METADATA_FIELDS:
        for record in records:
            value = getattr(record, field, None)
            if _has_metadata_value(field, value):
                best.setdefault(field, value)
                provider_values[record.provider][field] = value
    return best, {provider: dict(values) for provider, values in provider_values.items()}


def _median(values):
    values = sorted(value for value in values if isinstance(value, (int, float)) and value > 0)
    if not values:
        return None
    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint]
    return (values[midpoint - 1] + values[midpoint]) / 2


def _rent_to_value_ratio(records):
    ratios = []
    for record in records:
        rent = record.rent_estimate
        value = record.price_estimate or record.price
        if isinstance(rent, (int, float)) and rent > 0 and isinstance(value, (int, float)) and value > 0:
            ratios.append(rent / value)
    return _median(ratios)


def _estimate_values(record):
    values = {}
    for key, value in (record.price_estimates or {}).items():
        if isinstance(value, (int, float)) and value > 0:
            values[key] = value
    if isinstance(record.price_estimate, (int, float)) and record.price_estimate > 0:
        values.setdefault(f"{record.provider}_price_estimate", record.price_estimate)
    if isinstance(record.price, (int, float)) and record.price > 0:
        values.setdefault("listing_price", record.price)
    return values


def _numeric_sources(data):
    return {
        key: value
        for key, value in (data or {}).items()
        if isinstance(value, (int, float)) and value > 0
    }


def _direct_rent_estimates(records):
    direct = {}
    for record in records:
        sources = _numeric_sources(record.rent_estimates)
        if isinstance(record.rent_estimate, (int, float)) and record.rent_estimate > 0:
            sources.setdefault(f"{record.provider}_rent_estimate", record.rent_estimate)
        if sources:
            direct[record.provider] = {
                "monthly_rent_estimate": record.rent_estimate or next(iter(sources.values())),
                "sources": sources,
            }
    return direct


def _derived_rent_estimates(records, fallback_rent_ratio=None):
    cluster_ratio = _rent_to_value_ratio(records)
    ratio = cluster_ratio or fallback_rent_ratio
    if not ratio:
        return {}
    derived = {}
    for record in records:
        direct_rent = record.rent_estimate
        if isinstance(direct_rent, (int, float)) and direct_rent > 0:
            continue
        estimates = {}
        for estimate_name, value in _estimate_values(record).items():
            estimates[estimate_name] = {
                "monthly_rent_estimate": round(value * ratio, 2),
                "source_value": value,
            }
        if estimates:
            derived[record.provider] = {
                "rent_to_value_ratio": ratio,
                "ratio_source": "cluster_direct_rent" if cluster_ratio else "zip_median_direct_rent",
                "estimates": estimates,
            }
    return derived


def _cluster_summary(cluster, fallback_rent_ratio=None):
    providers = sorted({record.provider for record in cluster})
    prices = [record.price for record in cluster if isinstance(record.price, (int, float))]
    representative = cluster[0]
    best_metadata, provider_metadata = _best_metadata(cluster)
    tags = sorted({tag for record in cluster for tag in record.tags})
    home_types = sorted({record.normalized_home_type for record in cluster if record.normalized_home_type})
    return {
        "providers": providers,
        "provider_count": len(providers),
        "address": representative.address,
        "street_key": representative.street_key,
        "base_street_key": representative.base_street_key,
        "unit_key": representative.unit_key,
        "zip_code": representative.zip_code,
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "home_type": best_metadata.get("home_type") or representative.home_type,
        "normalized_home_types": home_types,
        "best_metadata": best_metadata,
        "provider_metadata": provider_metadata,
        "direct_rent_estimates": _direct_rent_estimates(cluster),
        "derived_rent_estimates": _derived_rent_estimates(cluster, fallback_rent_ratio=fallback_rent_ratio),
        "tags": tags,
        "records": [asdict(record) for record in cluster],
    }


def _price_delta(left, right):
    left_prices = [
        record.get("price") for record in left.get("records", [])
        if isinstance(record.get("price"), (int, float)) and record.get("price")
    ]
    right_prices = [
        record.get("price") for record in right.get("records", [])
        if isinstance(record.get("price"), (int, float)) and record.get("price")
    ]
    if not left_prices or not right_prices:
        return None
    left_price = min(left_prices)
    right_price = min(right_prices)
    return abs(left_price - right_price) / max(left_price, right_price)


def _cluster_coordinates(summary):
    best_metadata = summary.get("best_metadata") if isinstance(summary.get("best_metadata"), dict) else {}
    candidates = [
        (best_metadata.get("latitude"), best_metadata.get("longitude")),
    ]
    for record in summary.get("records", []):
        candidates.append((record.get("latitude"), record.get("longitude")))
    for latitude, longitude in candidates:
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            return float(latitude), float(longitude)
    return None


def _coordinate_delta_miles(left, right):
    left_coordinates = _cluster_coordinates(left)
    right_coordinates = _cluster_coordinates(right)
    if not left_coordinates or not right_coordinates:
        return None
    left_latitude, left_longitude = left_coordinates
    right_latitude, right_longitude = right_coordinates
    average_latitude = math.radians((left_latitude + right_latitude) / 2)
    latitude_miles = (left_latitude - right_latitude) * 69.0
    longitude_miles = (left_longitude - right_longitude) * 69.0 * math.cos(average_latitude)
    return math.sqrt(latitude_miles ** 2 + longitude_miles ** 2)


def _cluster_similarity(left, right):
    if left["zip_code"] != right["zip_code"]:
        return 0.0
    if left.get("unit_key") != right.get("unit_key"):
        return 0.0
    left_base = left.get("base_street_key") or left.get("street_key") or ""
    right_base = right.get("base_street_key") or right.get("street_key") or ""
    if not left_base or not right_base:
        return 0.0
    if _street_number(left_base) != _street_number(right_base):
        return 0.0
    score = SequenceMatcher(None, left_base, right_base).ratio()
    price_delta = _price_delta(left, right)
    if price_delta is not None:
        if price_delta <= 0.03:
            score += 0.05
        elif price_delta > 0.25:
            score -= 0.05
    return round(min(score, 1.0), 4)


def _auto_alignment_reason(left, right, score):
    price_delta = _price_delta(left, right)
    left_providers = set(left.get("providers", []))
    right_providers = set(right.get("providers", []))
    if left_providers & right_providers:
        return ""
    if len(left_providers) > 1 and len(right_providers) > 1:
        return ""
    if score >= 0.98:
        return "near_exact_address"
    left_base = left.get("base_street_key") or left.get("street_key") or ""
    right_base = right.get("base_street_key") or right.get("street_key") or ""
    left_directionless = _directionless_base_key(left_base)
    right_directionless = _directionless_base_key(right_base)
    left_directions = _direction_tokens(left_base)
    right_directions = _direction_tokens(right_base)
    compatible_directions = not left_directions or not right_directions or left_directions == right_directions
    same_directions = left_directions == right_directions
    coordinate_delta = _coordinate_delta_miles(left, right)
    coordinates_match = coordinate_delta is not None and coordinate_delta <= 0.03
    price_compatible = price_delta is not None and price_delta <= 0.03
    same_nonempty_unit = bool(left.get("unit_key")) and left.get("unit_key") == right.get("unit_key")
    if (
        same_nonempty_unit
        and _street_number(left_base)
        and _street_number(left_base) == _street_number(right_base)
        and left_directionless
        and left_directionless == right_directionless
        and compatible_directions
        and (same_directions or coordinates_match or price_compatible)
        and score >= 0.80
    ):
        return "same_unit_directional_variant"
    if price_delta is not None and price_delta <= 0.001 and score >= 0.88:
        return "same_price_same_street_number"
    if price_delta is not None and price_delta <= 0.001 and score >= 0.82:
        if left.get("unit_key") or right.get("unit_key"):
            return "same_price_same_unit"
        if _street_number(left_base) or len(set(left_base.split()) & set(right_base.split())) >= 1:
            return "same_price_sparse_address"
    if price_delta is not None and price_delta <= 0.03 and score >= 0.94:
        return "close_price_strong_address_similarity"
    if price_delta is None and score >= 0.97:
        return "strong_address_similarity"
    return ""


def _merge_clusters_with_evidence(initial_summaries, fallback_rent_ratio=None):
    parent = list(range(len(initial_summaries)))
    evidence = []

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left_index, right_index):
        left_root = find(left_index)
        right_root = find(right_index)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_index, left in enumerate(initial_summaries):
        for right_index in range(left_index + 1, len(initial_summaries)):
            right = initial_summaries[right_index]
            score = _cluster_similarity(left, right)
            if not score:
                continue
            reason = _auto_alignment_reason(left, right, score)
            if not reason:
                continue
            union(left_index, right_index)
            source = left if len(left["providers"]) == 1 else right
            candidate = right if source is left else left
            evidence.append({
                "score": score,
                "reason": reason,
                "source": {
                    "providers": source["providers"],
                    "address": source["address"],
                    "street_key": source["street_key"],
                    "price_min": source["price_min"],
                    "price_max": source["price_max"],
                    "records": source["records"][:3],
                },
                "candidate": {
                    "providers": candidate["providers"],
                    "address": candidate["address"],
                    "street_key": candidate["street_key"],
                    "price_min": candidate["price_min"],
                    "price_max": candidate["price_max"],
                    "records": candidate["records"][:3],
                },
            })

    grouped = defaultdict(list)
    for index, summary in enumerate(initial_summaries):
        grouped[find(index)].extend(summary["records"])
    merged_clusters = [
        [ReconciledRecord(**record) for record in records]
        for records in grouped.values()
    ]
    return [_cluster_summary(cluster, fallback_rent_ratio=fallback_rent_ratio) for cluster in merged_clusters], evidence


def _provider_only_near_misses(cluster_summaries, providers, threshold=0.82):
    counts = {provider: 0 for provider in providers}
    samples = {provider: [] for provider in providers}
    for summary in cluster_summaries:
        if len(summary["providers"]) != 1:
            continue
        provider = summary["providers"][0]
        candidates = []
        for candidate in cluster_summaries:
            if candidate is summary or provider in candidate["providers"]:
                continue
            score = _cluster_similarity(summary, candidate)
            if score >= threshold:
                candidates.append((score, candidate))
        if not candidates:
            continue
        candidates.sort(key=lambda item: item[0], reverse=True)
        counts[provider] += 1
        if len(samples[provider]) < 12:
            samples[provider].append({
                "score": candidates[0][0],
                "source": {
                    "providers": summary["providers"],
                    "address": summary["address"],
                    "street_key": summary["street_key"],
                    "price_min": summary["price_min"],
                    "price_max": summary["price_max"],
                    "records": summary["records"][:3],
                },
                "candidate": {
                    "providers": candidates[0][1]["providers"],
                    "address": candidates[0][1]["address"],
                    "street_key": candidates[0][1]["street_key"],
                    "price_min": candidates[0][1]["price_min"],
                    "price_max": candidates[0][1]["price_max"],
                    "records": candidates[0][1]["records"][:3],
                },
            })
    return {"counts": counts, "samples": samples}


def _absolute_provider_url(provider, url):
    url = str(url or "").strip()
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return f"{PROVIDER_URL_BASES.get(provider, '').rstrip('/')}{url}"
    return url


def _safe_filename_part(value, fallback="item", max_length=80):
    text = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "")).strip("_")
    return (text or fallback)[:max_length]


def _sample_debug_records(sample):
    items = []
    for role in ("source", "candidate"):
        side = sample.get(role) if isinstance(sample.get(role), dict) else {}
        records = side.get("records") if isinstance(side.get("records"), list) else []
        for record in records:
            provider = record.get("provider") or (side.get("providers") or [""])[0]
            url = _absolute_provider_url(provider, record.get("url"))
            if not provider or not url:
                continue
            items.append({
                "role": role,
                "provider": provider,
                "url": url,
                "address": record.get("address") or side.get("address") or "",
                "source_property_id": record.get("source_property_id") or "",
                "price": record.get("price"),
            })
    return items


def capture_reconciliation_debug_screenshots(
    report,
    diagnostics_dir="re_analyzer/Data/ScraperDiagnostics",
    max_cases=12,
    warmup_seconds=2.0,
    chrome_path="",
    chrome_user_data_dir="",
    window_rect=None,
    ignore_detection=True,
):
    if not report:
        return {"captured_pages": 0, "case_count": 0, "errors": ["missing report"]}
    if chrome_path:
        scraping_utility.CHROME_BINARY_EXECUTABLE_PATH = str(Path(chrome_path).expanduser().resolve())
    if chrome_user_data_dir:
        profile_dir = Path(chrome_user_data_dir).expanduser().resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        scraping_utility.CHROME_USER_DATA_DIR = str(profile_dir)
        scraping_utility.local_path_exists = True

    diagnostics_path = Path(diagnostics_dir) / "ReconciliationReview"
    case_limit = max(0, int(max_cases or 0))
    if case_limit <= 0:
        return {"captured_pages": 0, "case_count": 0, "errors": ["max_cases is zero"]}

    screenshot_cases = []
    visited_urls = {}
    captured_pages = 0
    errors = []

    try:
        with scraping_utility.get_selenium_driver(
            "about:blank",
            ignore_detection=ignore_detection,
            random_profile=False,
            clean_profile=False,
            window_rect=window_rect,
        ) as driver:
            for zip_report in report.get("zips", []):
                samples_by_provider = zip_report.get("sample_possible_misalignments") or {}
                for provider, samples in samples_by_provider.items():
                    for sample_index, sample in enumerate(samples or []):
                        if len(screenshot_cases) >= case_limit:
                            break
                        records = _sample_debug_records(sample)
                        if not records:
                            continue
                        case_key = (
                            zip_report.get("zip_code"),
                            sample.get("source", {}).get("address"),
                            sample.get("candidate", {}).get("address"),
                        )
                        case = {
                            "zip_code": zip_report.get("zip_code"),
                            "provider": provider,
                            "sample_index": sample_index,
                            "score": sample.get("score"),
                            "source_address": sample.get("source", {}).get("address"),
                            "candidate_address": sample.get("candidate", {}).get("address"),
                            "pages": [],
                        }
                        for record in records:
                            if record["url"] in visited_urls:
                                page_result = dict(visited_urls[record["url"]])
                                page_result["role"] = record["role"]
                                case["pages"].append(page_result)
                                continue
                            prefix = "_".join([
                                "reconcile_review",
                                _safe_filename_part(zip_report.get("zip_code"), "zip"),
                                _safe_filename_part(record["provider"], "provider"),
                                _safe_filename_part(record["role"], "role"),
                                _safe_filename_part(record.get("source_property_id") or record.get("address"), "property"),
                            ])
                            try:
                                driver.get(record["url"])
                                time.sleep(max(0.0, float(warmup_seconds or 0.0)))
                                diagnostic = save_page_diagnostics(
                                    driver,
                                    diagnostics_path,
                                    prefix,
                                    extra={
                                        "reason": "source_reconciliation_needs_review",
                                        "case_key": case_key,
                                        "record": record,
                                    },
                                )
                                page_result = {
                                    "saved": True,
                                    "role": record["role"],
                                    "provider": record["provider"],
                                    "address": record["address"],
                                    "url": record["url"],
                                    **diagnostic,
                                }
                                captured_pages += 1
                            except Exception as exc:
                                page_result = {
                                    "saved": False,
                                    "role": record["role"],
                                    "provider": record["provider"],
                                    "address": record["address"],
                                    "url": record["url"],
                                    "error": str(exc),
                                }
                                errors.append(f"{record['provider']} {record['url']}: {exc}")
                            visited_urls[record["url"]] = {
                                key: value for key, value in page_result.items()
                                if key != "role"
                            }
                            case["pages"].append(page_result)
                        sample["debug_screenshots"] = case["pages"]
                        screenshot_cases.append(case)
                    if len(screenshot_cases) >= case_limit:
                        break
                if len(screenshot_cases) >= case_limit:
                    break
    except Exception as exc:
        errors.append(str(exc))

    summary = {
        "generated_at": datetime.now().isoformat(),
        "diagnostics_dir": str(diagnostics_path),
        "case_count": len(screenshot_cases),
        "captured_pages": captured_pages,
        "max_cases": case_limit,
        "errors": errors[:20],
        "cases": screenshot_cases,
    }
    report["debug_screenshots"] = summary
    return summary


def _metadata_completeness(records, providers):
    by_provider = {}
    for provider in providers:
        provider_records = [record for record in records if record.provider == provider]
        field_stats = {}
        for field in METADATA_FIELDS:
            present = sum(1 for record in provider_records if _has_metadata_value(field, getattr(record, field, None)))
            total = len(provider_records)
            field_stats[field] = {
                "present": present,
                "missing": max(0, total - present),
                "total": total,
                "coverage_pct": round((present / total) * 100, 1) if total else 0,
            }
        by_provider[provider] = field_stats
    return by_provider


def _estimate_field_audit(records, providers):
    output = {}
    for provider in providers:
        provider_records = [record for record in records if record.provider == provider]
        rent_path_counts = Counter()
        value_path_counts = Counter()
        missing_rent_with_key_hits = 0
        missing_value_with_key_hits = 0
        direct_rent_listing_count = 0
        remarks_rent_count = 0
        raw_rent_key_record_count = 0
        raw_value_key_record_count = 0
        for record in provider_records:
            rent_hits = (record.estimate_key_audit or {}).get("rent_key_hits") or []
            value_hits = (record.estimate_key_audit or {}).get("value_key_hits") or []
            if rent_hits:
                raw_rent_key_record_count += 1
            if value_hits:
                raw_value_key_record_count += 1
            if not _has_metadata_value("rent_estimate", record.rent_estimate) and rent_hits:
                missing_rent_with_key_hits += 1
            if not _has_metadata_value("price_estimate", record.price_estimate) and value_hits:
                missing_value_with_key_hits += 1
            for hit in rent_hits:
                if hit.get("path"):
                    rent_path_counts[hit["path"]] += 1
            for hit in value_hits:
                if hit.get("path"):
                    value_path_counts[hit["path"]] += 1
            rent_sources = record.rent_estimates or {}
            if any(key.endswith("_rent_listing_price") for key in rent_sources):
                direct_rent_listing_count += 1
            if any(key.endswith("_remarks_rent_estimate") for key in rent_sources):
                remarks_rent_count += 1
        total = len(provider_records)
        output[provider] = {
            "total": total,
            "rent_estimate_present": sum(1 for record in provider_records if _has_metadata_value("rent_estimate", record.rent_estimate)),
            "price_estimate_present": sum(1 for record in provider_records if _has_metadata_value("price_estimate", record.price_estimate)),
            "raw_rent_key_record_count": raw_rent_key_record_count,
            "raw_value_key_record_count": raw_value_key_record_count,
            "missing_rent_with_raw_key_hits": missing_rent_with_key_hits,
            "missing_value_with_raw_key_hits": missing_value_with_key_hits,
            "direct_rent_listing_price_count": direct_rent_listing_count,
            "remarks_rent_estimate_count": remarks_rent_count,
            "top_rent_key_paths": [
                {"path": path, "count": count}
                for path, count in rent_path_counts.most_common(10)
            ],
            "top_value_key_paths": [
                {"path": path, "count": count}
                for path, count in value_path_counts.most_common(10)
            ],
        }
    return output


def _provider_only_class(summary):
    tags = set(summary.get("tags") or [])
    home_types = set(summary.get("normalized_home_types") or [])
    base_key = summary.get("base_street_key") or ""
    if "new_construction_plan" in tags or "to_be_built" in tags:
        return "new_construction_plan"
    if "undisclosed_or_missing_address" in tags:
        return "undisclosed_or_missing_address"
    if "intersection_or_generic_address" in tags:
        return "intersection_or_generic_address"
    if "land_or_lot" in tags or "land" in home_types:
        return "land_or_lot"
    if not _street_number(base_key) or len(base_key.split()) < 3:
        return "weak_address"
    return "true_source_only_candidate"


def _provider_only_classifications(cluster_summaries, providers):
    counts = {provider: Counter() for provider in providers}
    samples = {provider: defaultdict(list) for provider in providers}
    for summary in cluster_summaries:
        if len(summary.get("providers") or []) != 1:
            continue
        provider = summary["providers"][0]
        if provider not in counts:
            continue
        classification = _provider_only_class(summary)
        counts[provider][classification] += 1
        if len(samples[provider][classification]) < 8:
            samples[provider][classification].append(summary)
    return {
        "counts": {provider: dict(counts[provider]) for provider in providers},
        "samples": {
            provider: {label: list(items) for label, items in provider_samples.items()}
            for provider, provider_samples in samples.items()
        },
    }


def _add_metadata_totals(total_counter, report):
    for provider, fields in report.get("metadata_completeness", {}).items():
        for field, stats in fields.items():
            key = f"{provider}|{field}"
            total_counter[key]["present"] += stats.get("present", 0)
            total_counter[key]["total"] += stats.get("total", 0)


def _add_estimate_audit_totals(total_counter, path_counter, report):
    numeric_fields = (
        "total",
        "rent_estimate_present",
        "price_estimate_present",
        "raw_rent_key_record_count",
        "raw_value_key_record_count",
        "missing_rent_with_raw_key_hits",
        "missing_value_with_raw_key_hits",
        "direct_rent_listing_price_count",
        "remarks_rent_estimate_count",
    )
    for provider, stats in (report.get("estimate_field_audit") or {}).items():
        for field in numeric_fields:
            total_counter[f"{provider}|{field}"] += stats.get(field, 0)
        for item in stats.get("top_rent_key_paths") or []:
            path_counter[f"{provider}|rent"][item.get("path") or ""] += item.get("count", 0)
        for item in stats.get("top_value_key_paths") or []:
            path_counter[f"{provider}|value"][item.get("path") or ""] += item.get("count", 0)


def _finalize_metadata_totals(total_counter, providers):
    output = {}
    for provider in providers:
        output[provider] = {}
        for field in METADATA_FIELDS:
            stats = total_counter.get(f"{provider}|{field}", {"present": 0, "total": 0})
            present = stats["present"]
            total = stats["total"]
            output[provider][field] = {
                "present": present,
                "missing": max(0, total - present),
                "total": total,
                "coverage_pct": round((present / total) * 100, 1) if total else 0,
            }
    return output


def _finalize_estimate_audit_totals(total_counter, path_counter, providers):
    numeric_fields = (
        "total",
        "rent_estimate_present",
        "price_estimate_present",
        "raw_rent_key_record_count",
        "raw_value_key_record_count",
        "missing_rent_with_raw_key_hits",
        "missing_value_with_raw_key_hits",
        "direct_rent_listing_price_count",
        "remarks_rent_estimate_count",
    )
    output = {}
    for provider in providers:
        stats = {field: total_counter.get(f"{provider}|{field}", 0) for field in numeric_fields}
        stats["top_rent_key_paths"] = [
            {"path": path, "count": count}
            for path, count in path_counter.get(f"{provider}|rent", Counter()).most_common(10)
            if path
        ]
        stats["top_value_key_paths"] = [
            {"path": path, "count": count}
            for path, count in path_counter.get(f"{provider}|value", Counter()).most_common(10)
            if path
        ]
        output[provider] = stats
    return output


def reconcile_zip(zip_code, providers=DEFAULT_PROVIDERS, include_nearby=False):
    records = []
    provider_record_counts = {}
    provider_match_counts = {}
    for provider in providers:
        provider_records = [
            _record_from_dict(provider, item)
            for item in _load_canonical_records(provider, zip_code)
            if isinstance(item, dict)
        ]
        if not include_nearby:
            provider_records = [record for record in provider_records if record.zip_code == str(zip_code)]
        provider_records = _dedupe_provider_records(provider_records)
        records.extend(provider_records)
        provider_record_counts[provider] = len(provider_records)
        provider_match_counts[provider] = sum(1 for record in provider_records if record.zip_code == str(zip_code))

    fallback_rent_ratio = _rent_to_value_ratio(records)
    clusters = _cluster_records(records)
    initial_cluster_summaries = [_cluster_summary(cluster, fallback_rent_ratio=fallback_rent_ratio) for cluster in clusters]
    cluster_summaries, auto_alignment_samples = _merge_clusters_with_evidence(initial_cluster_summaries, fallback_rent_ratio=fallback_rent_ratio)
    all_provider_set = set(providers)
    by_presence = Counter(tuple(summary["providers"]) for summary in cluster_summaries)
    provider_only = {
        provider: sum(1 for summary in cluster_summaries if summary["providers"] == [provider])
        for provider in providers
    }
    derived_rent_estimate_count = sum(1 for summary in cluster_summaries if summary.get("derived_rent_estimates"))
    near_misses = _provider_only_near_misses(cluster_summaries, providers)
    provider_only_classifications = _provider_only_classifications(cluster_summaries, providers)
    auto_alignment_counts = {provider: 0 for provider in providers}
    for sample in auto_alignment_samples:
        source_providers = sample.get("source", {}).get("providers") or []
        if len(source_providers) == 1 and source_providers[0] in auto_alignment_counts:
            auto_alignment_counts[source_providers[0]] += 1
    all_provider_overlap = sum(1 for summary in cluster_summaries if set(summary["providers"]) == all_provider_set)
    direct_rent_estimate_count = sum(1 for summary in cluster_summaries if summary.get("direct_rent_estimates"))
    pair_overlap = {}
    for left, right in combinations(providers, 2):
        pair_overlap[f"{left}_{right}"] = sum(
            1 for summary in cluster_summaries
            if left in summary["providers"] and right in summary["providers"]
        )

    return {
        "zip_code": str(zip_code),
        "provider_record_counts": provider_record_counts,
        "provider_requested_zip_counts": provider_match_counts,
        "initial_cluster_count": len(initial_cluster_summaries),
        "cluster_count": len(cluster_summaries),
        "all_provider_overlap": all_provider_overlap,
        "pair_overlap": pair_overlap,
        "provider_only": provider_only,
        "provider_only_classifications": provider_only_classifications,
        "auto_aligned_counts": auto_alignment_counts,
        "direct_rent_estimate_count": direct_rent_estimate_count,
        "derived_rent_estimate_count": derived_rent_estimate_count,
        "possible_misalignment_counts": near_misses["counts"],
        "metadata_completeness": _metadata_completeness(records, providers),
        "estimate_field_audit": _estimate_field_audit(records, providers),
        "zip_median_monthly_rent_to_value_ratio": fallback_rent_ratio,
        "presence_counts": {"|".join(key): value for key, value in by_presence.items()},
        "sample_all_provider": [
            summary for summary in cluster_summaries
            if set(summary["providers"]) == all_provider_set
        ][:10],
        "sample_provider_only": {
            provider: [
                summary for summary in cluster_summaries
                if summary["providers"] == [provider]
            ][:10]
            for provider in providers
        },
        "sample_auto_aligned": auto_alignment_samples[:30],
        "sample_possible_misalignments": near_misses["samples"],
    }


def reconcile_sources(zip_codes, providers=DEFAULT_PROVIDERS, include_nearby=False):
    zip_reports = [reconcile_zip(zip_code, providers=providers, include_nearby=include_nearby) for zip_code in zip_codes]
    totals = {
        "provider_record_counts": Counter(),
        "provider_requested_zip_counts": Counter(),
        "provider_only": Counter(),
        "auto_aligned_counts": Counter(),
        "possible_misalignment_counts": Counter(),
        "pair_overlap": Counter(),
        "initial_cluster_count": 0,
        "cluster_count": 0,
        "all_provider_overlap": 0,
        "derived_rent_estimate_count": 0,
        "direct_rent_estimate_count": 0,
    }
    metadata_totals = defaultdict(lambda: {"present": 0, "total": 0})
    estimate_audit_totals = Counter()
    estimate_audit_path_totals = defaultdict(Counter)
    classification_totals = {provider: Counter() for provider in providers}
    rent_ratios = []
    for report in zip_reports:
        totals["provider_record_counts"].update(report["provider_record_counts"])
        totals["provider_requested_zip_counts"].update(report["provider_requested_zip_counts"])
        totals["provider_only"].update(report["provider_only"])
        totals["auto_aligned_counts"].update(report["auto_aligned_counts"])
        totals["possible_misalignment_counts"].update(report["possible_misalignment_counts"])
        totals["pair_overlap"].update(report["pair_overlap"])
        totals["initial_cluster_count"] += report["initial_cluster_count"]
        totals["cluster_count"] += report["cluster_count"]
        totals["all_provider_overlap"] += report["all_provider_overlap"]
        totals["direct_rent_estimate_count"] += report.get("direct_rent_estimate_count", 0)
        totals["derived_rent_estimate_count"] += report.get("derived_rent_estimate_count", 0)
        _add_metadata_totals(metadata_totals, report)
        _add_estimate_audit_totals(estimate_audit_totals, estimate_audit_path_totals, report)
        if report.get("zip_median_monthly_rent_to_value_ratio"):
            rent_ratios.append(report["zip_median_monthly_rent_to_value_ratio"])
        for provider, class_counts in (report.get("provider_only_classifications", {}).get("counts") or {}).items():
            if provider in classification_totals:
                classification_totals[provider].update(class_counts)

    return {
        "generated_at": datetime.now().isoformat(),
        "zip_codes": [str(zip_code) for zip_code in zip_codes],
        "providers": list(providers),
        "include_nearby": include_nearby,
        "totals": {
            key: dict(value) if isinstance(value, Counter) else value
            for key, value in totals.items()
        },
        "provider_only_classification_counts": {
            provider: dict(classification_totals.get(provider, {}))
            for provider in providers
        },
        "metadata_completeness": _finalize_metadata_totals(metadata_totals, providers),
        "estimate_field_audit": _finalize_estimate_audit_totals(
            estimate_audit_totals,
            estimate_audit_path_totals,
            providers,
        ),
        "monthly_rent_to_value_ratio": _median(rent_ratios),
        "zips": zip_reports,
    }


def save_reconciliation_report(report):
    output_dir = Path(DATA_PATH) / "Fetched" / "Reconciliation"
    ensure_directory_exists(str(output_dir))
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    json_path = output_dir / f"source_reconciliation_{timestamp}.json"
    csv_path = output_dir / f"source_reconciliation_{timestamp}.csv"
    save_json(report, str(json_path))
    with open(csv_path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=[
            "zip_code",
            "initial_cluster_count",
            "cluster_count",
            "all_provider_overlap",
            "zillow_count",
            "redfin_count",
            "realtor_count",
            "zillow_auto_aligned",
            "redfin_auto_aligned",
            "realtor_auto_aligned",
            "zillow_only",
            "redfin_only",
            "realtor_only",
            "zillow_possible_misaligned",
            "redfin_possible_misaligned",
            "realtor_possible_misaligned",
            "zillow_redfin",
            "zillow_realtor",
            "redfin_realtor",
        ])
        writer.writeheader()
        for item in report["zips"]:
            writer.writerow({
                "zip_code": item["zip_code"],
                "initial_cluster_count": item.get("initial_cluster_count", item["cluster_count"]),
                "cluster_count": item["cluster_count"],
                "all_provider_overlap": item["all_provider_overlap"],
                "zillow_count": item["provider_record_counts"].get("zillow", 0),
                "redfin_count": item["provider_record_counts"].get("redfin", 0),
                "realtor_count": item["provider_record_counts"].get("realtor", 0),
                "zillow_auto_aligned": item.get("auto_aligned_counts", {}).get("zillow", 0),
                "redfin_auto_aligned": item.get("auto_aligned_counts", {}).get("redfin", 0),
                "realtor_auto_aligned": item.get("auto_aligned_counts", {}).get("realtor", 0),
                "zillow_only": item["provider_only"].get("zillow", 0),
                "redfin_only": item["provider_only"].get("redfin", 0),
                "realtor_only": item["provider_only"].get("realtor", 0),
                "zillow_possible_misaligned": item["possible_misalignment_counts"].get("zillow", 0),
                "redfin_possible_misaligned": item["possible_misalignment_counts"].get("redfin", 0),
                "realtor_possible_misaligned": item["possible_misalignment_counts"].get("realtor", 0),
                "zillow_redfin": item["pair_overlap"].get("zillow_redfin", 0),
                "zillow_realtor": item["pair_overlap"].get("zillow_realtor", 0),
                "redfin_realtor": item["pair_overlap"].get("redfin_realtor", 0),
            })
    return {"json_path": str(json_path), "csv_path": str(csv_path)}


def parse_args():
    parser = argparse.ArgumentParser(description="Reconcile saved canonical listings across providers.")
    parser.add_argument("--zip-code", action="append", required=True, help="ZIP code to reconcile. Repeat for multiple ZIPs.")
    parser.add_argument("--providers", nargs="+", choices=DEFAULT_PROVIDERS, default=list(DEFAULT_PROVIDERS))
    parser.add_argument("--include-nearby", action="store_true", help="Include records whose parsed ZIP differs from the requested ZIP.")
    parser.add_argument("--save", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--debug-screenshots", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--debug-screenshot-limit", type=int, default=12)
    parser.add_argument("--debug-screenshot-warmup-seconds", type=float, default=2.0)
    parser.add_argument("--diagnostics-dir", default="re_analyzer/Data/ScraperDiagnostics")
    parser.add_argument("--chrome-path", default="")
    return parser.parse_args()


def main():
    args = parse_args()
    report = reconcile_sources(args.zip_code, providers=tuple(args.providers), include_nearby=args.include_nearby)
    if args.debug_screenshots:
        capture_reconciliation_debug_screenshots(
            report,
            diagnostics_dir=args.diagnostics_dir,
            max_cases=args.debug_screenshot_limit,
            warmup_seconds=args.debug_screenshot_warmup_seconds,
            chrome_path=args.chrome_path,
        )
    if args.save:
        report["saved_paths"] = save_reconciliation_report(report)
    print("SOURCE_RECONCILIATION_SUMMARY")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
