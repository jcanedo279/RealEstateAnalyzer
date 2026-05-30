import hashlib
import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


_SPACE_RE = re.compile(r"\s+")
_UNIT_RE = re.compile(r"\b(?:apt|apartment|unit|ste|suite|#)\s*([a-z0-9-]+)\b", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^a-z0-9\s#-]")


@dataclass(frozen=True)
class CanonicalListing:
    source_name: str
    source_property_id: str
    canonical_property_id: str
    address: str = ""
    normalized_address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    price: Optional[float] = None
    price_estimate: Optional[float] = None
    rent_estimate: Optional[float] = None
    price_estimates: Optional[Dict[str, Any]] = None
    rent_estimates: Optional[Dict[str, Any]] = None
    estimate_history: Optional[List[Dict[str, Any]]] = None
    price_history: Optional[List[Dict[str, Any]]] = None
    tax_history: Optional[List[Dict[str, Any]]] = None
    home_type: str = ""
    beds: Optional[float] = None
    baths: Optional[float] = None
    living_area: Optional[float] = None
    lot_size: Optional[float] = None
    year_built: Optional[int] = None
    status: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    provider_metadata: Optional[Dict[str, Any]] = None
    url: str = ""
    raw: Optional[Dict[str, Any]] = None

    def to_dict(self):
        return asdict(self)


def normalize_zip_code(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    return digits[:5].zfill(5)


def normalize_address(address: str) -> str:
    if not address:
        return ""
    normalized = address.lower().replace("&", " and ")
    normalized = _UNIT_RE.sub(r" unit \1", normalized)
    normalized = _PUNCT_RE.sub(" ", normalized)
    normalized = _SPACE_RE.sub(" ", normalized).strip()
    return normalized


def canonical_property_identity(
    source_name: str,
    source_property_id: str = "",
    address: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
    home_type: str = "",
) -> str:
    normalized_address = normalize_address(address)
    normalized_zip = normalize_zip_code(zip_code)
    address_parts = [
        normalized_address,
        (city or "").strip().lower(),
        (state or "").strip().lower(),
        normalized_zip,
        (home_type or "").strip().lower(),
    ]
    if normalized_address and (city or normalized_zip):
        key = f"address|{'|'.join(address_parts)}"
    else:
        key = f"source|{(source_name or '').strip().lower()}|{str(source_property_id or '').strip().lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def parse_city_state_zip_from_address(address: str):
    if not address:
        return "", "", ""
    parts = [part.strip() for part in address.split(",") if part.strip()]
    if len(parts) < 2:
        return "", "", ""
    city = parts[-2] if len(parts) >= 2 else ""
    state_zip = parts[-1] if parts else ""
    match = re.search(r"\b([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b", state_zip, re.IGNORECASE)
    if not match:
        return city, "", normalize_zip_code(state_zip)
    return city, match.group(1).upper(), normalize_zip_code(match.group(2))
