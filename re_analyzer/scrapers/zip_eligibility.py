import argparse
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from re_analyzer.scrapers import scraping_utility
from re_analyzer.scrapers.property_identity import normalize_zip_code
from re_analyzer.utility.utility import DATA_PATH, ensure_directory_exists, load_json, save_json


HUD_USPS_API_URL = "https://www.huduser.gov/hudapi/public/usps"
DEFAULT_STATE = "FL"
DEFAULT_CROSSWALK_TYPE = 1
ELIGIBILITY_DIR = Path(DATA_PATH) / "Fetched" / "ZipEligibility"
HUD_USPS_ELIGIBILITY_PATH = ELIGIBILITY_DIR / "hud_usps_zip_eligibility.json"
TOKEN_ENV_NAMES = (
    "HUD_USPS_CROSSWALK_TOKEN",
    "HUD_USER_API_TOKEN",
    "HUD_API_TOKEN",
)


def _number(value):
    if isinstance(value, bool) or value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return 0.0


def _token_from_env():
    for name in TOKEN_ENV_NAMES:
        token = os.environ.get(name, "").strip()
        if token:
            return token
    return ""


def _zip_from_row(row):
    for key in ("zip", "ZIP", "zipcode", "zip_code", "usps_zip", "input", "query"):
        value = row.get(key)
        zip_code = normalize_zip_code(value)
        if zip_code:
            return zip_code
    for value in row.values():
        text = str(value or "")
        match = re.fullmatch(r"\d{5}", text)
        if match:
            return text
    return ""


def _extract_results(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        results = data.get("results") or []
        return results if isinstance(results, list) else []
    if isinstance(data, list):
        rows = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("results"), list):
                rows.extend(item["results"])
            elif isinstance(item, dict):
                rows.append(item)
        return rows
    return []


def fetch_hud_usps_crosswalk(token=None, state=DEFAULT_STATE, crosswalk_type=DEFAULT_CROSSWALK_TYPE, year=None, quarter=None):
    token = token or _token_from_env()
    if not token:
        raise ValueError(
            "HUD-USPS crosswalk token missing. Set HUD_USPS_CROSSWALK_TOKEN or HUD_USER_API_TOKEN."
        )
    params = {
        "type": str(crosswalk_type),
        "query": state,
    }
    if year:
        params["year"] = str(year)
    if quarter:
        params["quarter"] = str(quarter)
    url = f"{HUD_USPS_API_URL}?{urlencode(params)}"
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HUD-USPS API returned {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"HUD-USPS API request failed: {exc}") from exc


def _build_entries(rows, requested_zip_codes=None, min_residential_or_other_ratio=0.0):
    requested = [normalize_zip_code(zip_code) for zip_code in (requested_zip_codes or [])]
    requested = [zip_code for zip_code in requested if zip_code]
    by_zip = defaultdict(lambda: {
        "row_count": 0,
        "res_ratio_sum": 0.0,
        "bus_ratio_sum": 0.0,
        "oth_ratio_sum": 0.0,
        "tot_ratio_sum": 0.0,
        "city": "",
        "state": "",
    })

    for row in rows:
        if not isinstance(row, dict):
            continue
        zip_code = _zip_from_row(row)
        if not zip_code:
            continue
        item = by_zip[zip_code]
        item["row_count"] += 1
        item["res_ratio_sum"] += _number(row.get("res_ratio") or row.get("RES_RATIO"))
        item["bus_ratio_sum"] += _number(row.get("bus_ratio") or row.get("BUS_RATIO"))
        item["oth_ratio_sum"] += _number(row.get("oth_ratio") or row.get("OTH_RATIO"))
        item["tot_ratio_sum"] += _number(row.get("tot_ratio") or row.get("TOTAL_RATIO") or row.get("TOT_RATIO"))
        item["city"] = item["city"] or str(row.get("city") or row.get("USPS_ZIP_PREF_CITY") or "")
        item["state"] = item["state"] or str(row.get("state") or row.get("USPS_ZIP_PREF_STATE") or "")

    all_zip_codes = sorted(set(requested) | set(by_zip.keys()))
    entries = {}
    threshold = max(0.0, float(min_residential_or_other_ratio or 0.0))
    for zip_code in all_zip_codes:
        item = by_zip.get(zip_code)
        if not item:
            entries[zip_code] = {
                "eligible": False,
                "reason": "absent_from_hud_usps_crosswalk",
                "row_count": 0,
                "res_ratio_sum": 0.0,
                "bus_ratio_sum": 0.0,
                "oth_ratio_sum": 0.0,
                "tot_ratio_sum": 0.0,
            }
            continue
        residential_or_other_ratio = item["res_ratio_sum"] + item["oth_ratio_sum"]
        eligible = residential_or_other_ratio > threshold
        entries[zip_code] = {
            "eligible": eligible,
            "reason": "eligible_residential_or_other_ratio" if eligible else "no_residential_or_other_address_ratio",
            "row_count": item["row_count"],
            "res_ratio_sum": round(item["res_ratio_sum"], 8),
            "bus_ratio_sum": round(item["bus_ratio_sum"], 8),
            "oth_ratio_sum": round(item["oth_ratio_sum"], 8),
            "tot_ratio_sum": round(item["tot_ratio_sum"], 8),
            "residential_or_other_ratio_sum": round(residential_or_other_ratio, 8),
            "city": item["city"],
            "state": item["state"],
        }
    return entries


def build_hud_usps_zip_eligibility(
    token=None,
    state=DEFAULT_STATE,
    year=None,
    quarter=None,
    requested_zip_codes=None,
    save=True,
    min_residential_or_other_ratio=0.0,
):
    payload = fetch_hud_usps_crosswalk(
        token=token,
        state=state,
        crosswalk_type=DEFAULT_CROSSWALK_TYPE,
        year=year,
        quarter=quarter,
    )
    rows = _extract_results(payload)
    entries = _build_entries(
        rows,
        requested_zip_codes=requested_zip_codes,
        min_residential_or_other_ratio=min_residential_or_other_ratio,
    )
    counts = Counter("eligible" if item.get("eligible") else item.get("reason") for item in entries.values())
    report = {
        "generated_at": datetime.now().isoformat(),
        "source": "HUD-USPS ZIP Code Crosswalk",
        "state": state,
        "year": year,
        "quarter": quarter,
        "crosswalk_type": "zip-tract",
        "description": (
            "Eligibility is true when the HUD-USPS ZIP-Tract crosswalk has positive residential "
            "or other-address ratio for the ZIP. PO Box-only ZIPs generally do not appear in the "
            "crosswalk, and business-only ZIPs are excluded for listing scrapes."
        ),
        "source_url": "https://www.huduser.gov/portal/dataset/uspszip-api.html",
        "input_zip_count": len(requested_zip_codes or []),
        "entry_count": len(entries),
        "eligible_count": counts.get("eligible", 0),
        "ineligible_count": len(entries) - counts.get("eligible", 0),
        "reason_counts": dict(counts),
        "entries": entries,
    }
    if save:
        ensure_directory_exists(str(ELIGIBILITY_DIR))
        save_json(report, str(HUD_USPS_ELIGIBILITY_PATH))
    return report


def load_hud_usps_zip_eligibility():
    if not HUD_USPS_ELIGIBILITY_PATH.exists():
        return {}
    report = load_json(str(HUD_USPS_ELIGIBILITY_PATH))
    return report if isinstance(report, dict) else {}


def ensure_hud_usps_zip_eligibility_cache(
    *,
    token=None,
    state=DEFAULT_STATE,
    requested_zip_codes=None,
    min_residential_or_other_ratio=0.0,
):
    """
    Ensure the HUD-USPS eligibility cache exists.

    If the cache file is missing but a HUD token is available in env vars, build
    the cache automatically. Returns (report, metadata).
    """
    if HUD_USPS_ELIGIBILITY_PATH.exists():
        report = load_hud_usps_zip_eligibility()
        return report, {"auto_built": False, "cache_path": str(HUD_USPS_ELIGIBILITY_PATH)}

    token = token or _token_from_env()
    if not token:
        return {}, {
            "auto_built": False,
            "auto_build_attempted": False,
            "auto_build_error": "missing_hud_token",
            "cache_path": str(HUD_USPS_ELIGIBILITY_PATH),
        }

    try:
        report = build_hud_usps_zip_eligibility(
            token=token,
            state=state,
            requested_zip_codes=requested_zip_codes,
            save=True,
            min_residential_or_other_ratio=min_residential_or_other_ratio,
        )
        return report, {
            "auto_built": True,
            "auto_build_attempted": True,
            "cache_path": str(HUD_USPS_ELIGIBILITY_PATH),
            "cache_generated_at": report.get("generated_at"),
        }
    except Exception as exc:
        return {}, {
            "auto_built": False,
            "auto_build_attempted": True,
            "auto_build_error": str(exc),
            "cache_path": str(HUD_USPS_ELIGIBILITY_PATH),
        }


def filter_zip_codes_for_scrape(
    zip_codes,
    filter_name="hud_usps",
    min_residential_or_other_ratio=0.0,
    *,
    auto_build=True,
):
    zip_codes = [normalize_zip_code(zip_code) for zip_code in zip_codes]
    zip_codes = [zip_code for zip_code in zip_codes if zip_code]
    if filter_name in {"", "none", None}:
        return zip_codes, {
            "filter": "none",
            "applied": False,
            "input_count": len(zip_codes),
            "selected_count": len(zip_codes),
            "skipped_count": 0,
            "reason_counts": {},
        }

    report = {}
    cache_meta = {"cache_path": str(HUD_USPS_ELIGIBILITY_PATH)}
    if auto_build:
        report, cache_meta = ensure_hud_usps_zip_eligibility_cache(
            requested_zip_codes=zip_codes,
            min_residential_or_other_ratio=min_residential_or_other_ratio,
        )
    else:
        report = load_hud_usps_zip_eligibility()
    entries = report.get("entries") if isinstance(report.get("entries"), dict) else {}
    if not entries:
        return zip_codes, {
            "filter": filter_name,
            "applied": False,
            "input_count": len(zip_codes),
            "selected_count": len(zip_codes),
            "skipped_count": 0,
            "reason_counts": {"missing_cached_hud_usps_eligibility": len(zip_codes)},
            **cache_meta,
        }

    threshold = max(0.0, float(min_residential_or_other_ratio or 0.0))
    selected = []
    skipped = []
    reason_counts = Counter()
    for zip_code in zip_codes:
        entry = entries.get(zip_code)
        if not entry:
            skipped.append(zip_code)
            reason_counts["missing_from_cached_hud_usps_eligibility"] += 1
            continue
        residential_or_other_ratio = _number(entry.get("residential_or_other_ratio_sum"))
        eligible = bool(entry.get("eligible")) and residential_or_other_ratio > threshold
        if eligible:
            selected.append(zip_code)
        else:
            skipped.append(zip_code)
            reason_counts[entry.get("reason") or "ineligible"] += 1

    return selected, {
        "filter": filter_name,
        "applied": True,
        "input_count": len(zip_codes),
        "selected_count": len(selected),
        "skipped_count": len(skipped),
        "reason_counts": dict(reason_counts),
        "skipped_sample": skipped[:25],
        **cache_meta,
        "cache_generated_at": report.get("generated_at"),
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Build or inspect ZIP scrape eligibility from HUD-USPS Crosswalk data.")
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--year")
    parser.add_argument("--quarter")
    parser.add_argument("--token", default="")
    parser.add_argument("--zip-code", action="append", default=[])
    parser.add_argument("--save", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--check-cache", action="store_true", help="Only filter requested ZIPs using the cached eligibility file.")
    parser.add_argument("--min-residential-or-other-ratio", type=float, default=0.0)
    return parser.parse_args()


def main():
    args = parse_args()
    zip_codes = args.zip_code or [str(zip_code) for zip_code in scraping_utility.load_search_zip_codes()]
    if args.check_cache:
        selected, summary = filter_zip_codes_for_scrape(
            zip_codes,
            min_residential_or_other_ratio=args.min_residential_or_other_ratio,
        )
        print(json.dumps({"selected_zip_codes": selected, "summary": summary}, indent=2, sort_keys=True))
        return
    report = build_hud_usps_zip_eligibility(
        token=args.token,
        state=args.state,
        year=args.year,
        quarter=args.quarter,
        requested_zip_codes=zip_codes,
        save=args.save,
        min_residential_or_other_ratio=args.min_residential_or_other_ratio,
    )
    print(json.dumps({
        key: value for key, value in report.items()
        if key != "entries"
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
