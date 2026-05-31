import argparse
import fcntl
import json
import os
import random
import re
import shutil
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from re_analyzer.scrapers import scraping_utility
from re_analyzer.scrapers.page_diagnostics import detect_challenge, save_page_diagnostics, wait_for_manual_challenge
from re_analyzer.scrapers.provider_adapters import (
    ProviderBlockedError,
    RealtorListingProvider,
    RedfinListingProvider,
    UnsupportedProviderRegionError,
    ZillowListingProvider,
)
from re_analyzer.scrapers.injection_manifest import write_zip_injection_manifest
from re_analyzer.scrapers.zip_eligibility import filter_zip_codes_for_scrape
from re_analyzer.utility.utility import (
    DATA_PATH,
    PROPERTY_DETAILS_PATH,
    SEARCH_LISTINGS_METADATA_PATH,
    ensure_directory_exists,
    is_within_cooldown_period,
    load_json,
    random_delay,
    save_json,
)


DEFAULT_PAGE_LIMIT = 1
DEFAULT_ZIP_LIMIT = 1
KNOWN_PROVIDERS = ("zillow", "redfin", "realtor")
CHROME_CACHE_RELATIVE_PATHS = [
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "ShaderCache",
    "GrShaderCache",
    "GraphiteDawnCache",
    "component_crx_cache",
]
CHROME_PROFILE_CACHE_NAMES = {
    "Cache",
    "Code Cache",
    "GPUCache",
}


@dataclass
class ScrapeRunSummary:
    provider: str
    dry_run: bool
    zip_codes_requested: int
    zip_codes_processed: int
    zip_eligibility_filter: dict
    pages_requested: int
    pages_processed: int
    raw_listings_seen: int
    canonical_listings_seen: int
    saved_zip_codes: int
    empty_pages: int
    warnings: list
    errors: list
    diagnostics: list


@dataclass
class ProviderBackoffState:
    blocked_until_epoch: float = 0.0
    block_count: int = 0
    last_reason: str = ""


_DEAD_SESSION_EXC_NAMES = frozenset({"NoSuchWindowException", "InvalidSessionIdException"})


def _is_dead_session(exc: BaseException) -> bool:
    """True when Chrome's window or session is gone — all further driver calls will fail."""
    if type(exc).__name__ in _DEAD_SESSION_EXC_NAMES:
        return True
    msg = str(exc).lower()
    return (
        "target window already closed" in msg
        or "invalid session id" in msg
        or "web view not found" in msg
    )


def _apply_block_backoff(args, state: ProviderBackoffState, provider_name: str, *, reason: str, extra_seconds: float = 0.0):
    """
    Backoff helper to reduce repeated blocked requests.

    Sleeps locally and does not attempt to bypass provider challenges automatically.
    """
    base_seconds = float(getattr(args, "blocked_backoff_base_seconds", 0.0) or 0.0)
    if base_seconds <= 0:
        return
    max_seconds = float(getattr(args, "blocked_backoff_max_seconds", 600.0) or 600.0)
    if provider_name == "realtor" and max_seconds < 1800.0:
        max_seconds = 1800.0
    multiplier = float(getattr(args, "blocked_backoff_multiplier", 2.0) or 2.0)
    jitter_seconds = float(getattr(args, "blocked_backoff_jitter_seconds", 3.0) or 0.0)

    state.block_count += 1
    state.last_reason = str(reason or "")
    delay = base_seconds * (multiplier ** max(0, state.block_count - 1))
    delay = min(max_seconds, max(base_seconds, delay))
    if extra_seconds:
        delay = min(max_seconds, delay + float(extra_seconds))
    if jitter_seconds > 0:
        delay += random.uniform(0, jitter_seconds)

    state.blocked_until_epoch = max(state.blocked_until_epoch, time.time() + delay)
    print(f"[runner] backing off {provider_name} for ~{delay:.1f}s (reason={state.last_reason}, count={state.block_count})", flush=True)
    time.sleep(delay)


def _check_max_consecutive_blocks(args, state: ProviderBackoffState, provider_name: str) -> bool:
    """Return True if the run should be aborted due to too many consecutive blocks."""
    max_consecutive = int(getattr(args, "max_consecutive_blocks", 0) or 0)
    if max_consecutive > 0 and state.block_count >= max_consecutive:
        print(
            f"[runner] aborting {provider_name} run after {state.block_count} consecutive blocks "
            f"(limit={max_consecutive}, last_reason={state.last_reason!r}). "
            "The next run will auto-resume from the next ZIP.",
            flush=True,
        )
        scraping_utility.send_sms_alert(
            f"[{provider_name}] hard-blocked x{state.block_count}: {state.last_reason}"
        )
        return True
    return False


def _maybe_wait_for_backoff(state: ProviderBackoffState, provider_name: str):
    now = time.time()
    if state.blocked_until_epoch <= now:
        return
    remaining = max(0.0, state.blocked_until_epoch - now)
    if remaining:
        print(f"[runner] waiting {remaining:.1f}s due to prior {provider_name} block", flush=True)
        time.sleep(remaining)


def _zillow_property_details_path(zip_code: str, zpid: str) -> str:
    return os.path.join(PROPERTY_DETAILS_PATH, str(zip_code), f"{zpid}_property_details.json")


def _zillow_absolute_url(url: str, zpid: str) -> str:
    url = str(url or "").strip()
    if not url:
        zpid = str(zpid or "").strip()
        return f"https://www.zillow.com/homedetails/{zpid}_zpid/" if zpid else ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"https://www.zillow.com{url}"
    return f"https://www.zillow.com/{url}"


def _extract_next_data_json(page_source: str):
    match = re.search(
        r'<script[^>]+id=["\\\']__NEXT_DATA__["\\\'][^>]*>(?P<payload>.*?)</script>',
        page_source or "",
        re.DOTALL,
    )
    if not match:
        return None
    try:
        return json.loads(match.group("payload"))
    except json.JSONDecodeError:
        return None


def _is_zillow_property_page_payload(next_data: dict) -> bool:
    if not isinstance(next_data, dict):
        return False
    props = next_data.get("props") if isinstance(next_data.get("props"), dict) else {}
    page_props = props.get("pageProps") if isinstance(props.get("pageProps"), dict) else {}
    component_props = page_props.get("componentProps") if isinstance(page_props.get("componentProps"), dict) else {}
    if not component_props:
        return False
    # Strong signal used by the historical property-detail scraper.
    if "gdpClientCache" in component_props:
        return True
    # Fallback: accept other GDP-ish component props shapes.
    return any("gdp" in str(key).lower() for key in component_props.keys())


def _detail_verification_state(driver):
    """
    Detail-page verification detection.

    We intentionally avoid using `detect_challenge()` here because its heuristics
    are tuned to search/listing pages and can false-positive on valid property
    detail pages.
    """
    try:
        text = (driver.execute_script("return document.body ? document.body.innerText : '';") or "")[:4000]
    except Exception:
        text = ""
    haystack = (text or "").lower()
    markers = (
        "press and hold",
        "press & hold",
        "verify you are human",
        "unusual traffic",
        "automated requests",
        "are you a robot",
        "access denied",
        "forbidden",
        "blocked",
        "captcha",
    )
    hit = next((marker for marker in markers if marker in haystack), "")
    next_data_present = False
    try:
        next_data_present = bool(driver.execute_script("return Boolean(document.getElementById('__NEXT_DATA__'))"))
    except Exception:
        next_data_present = False
    return {
        "is_verification": bool(hit) and not next_data_present,
        "marker": hit,
        "next_data_present": next_data_present,
        "body_text_excerpt": text[:1200],
    }


def _fetch_zillow_property_details_for_zip(driver, zip_code: str, canonical_listings: list, args):
    if not canonical_listings:
        return {"enabled": True, "attempted": 0, "saved": 0, "skipped": 0, "errors": []}

    limit = max(0, int(getattr(args, "zillow_property_details_limit_per_zip", 0) or 0))
    cooldown_hours = float(getattr(args, "zillow_property_details_cooldown_hours", 36.0) or 36.0)
    delay_seconds = max(0.0, float(getattr(args, "zillow_property_details_delay_seconds", 2.0) or 0.0))
    force = bool(getattr(args, "zillow_property_details_force", False))

    attempted = 0
    saved = 0
    skipped = 0
    blocked = 0
    errors = []

    for listing in canonical_listings:
        if limit and attempted >= limit:
            break
        zpid = str(getattr(listing, "source_property_id", "") or "").strip()
        if not zpid:
            continue

        attempted += 1
        out_path = _zillow_property_details_path(zip_code, zpid)
        ensure_directory_exists(os.path.dirname(out_path))

        if not force and os.path.exists(out_path):
            try:
                existing = load_json(out_path)
            except Exception:
                existing = {}
            checked_at = (existing or {}).get("last_checked")
            if checked_at and is_within_cooldown_period(checked_at, timedelta(hours=max(0, cooldown_hours))):
                skipped += 1
                continue

        url = _zillow_absolute_url(getattr(listing, "url", "") or "", zpid)
        if not url:
            skipped += 1
            continue

        try:
            driver.get(url)
            if delay_seconds:
                random_delay(max(0.1, delay_seconds * 0.8), delay_seconds * 1.2)

            # If the property page triggers a verification/challenge, do not proceed.
            # This is intentionally not a bypass mechanism: we either wait for manual
            # resolution (if configured) or skip to avoid repeated blocked requests.
            verification = _detail_verification_state(driver)
            if verification.get("is_verification"):
                manual_wait = float(getattr(args, "manual_challenge_wait_seconds", 0) or 0)
                if manual_wait > 0:
                    wait_for_manual_challenge(driver, manual_wait, poll_seconds=2.0)
                    verification = _detail_verification_state(driver)
                if verification.get("is_verification"):
                    blocked += 1
                    if getattr(args, "debug_snapshots", False):
                        save_page_diagnostics(
                            driver,
                            args.diagnostics_dir,
                            f"zillow_{zip_code}_property_{zpid}_challenge",
                            extra={"zip_code": str(zip_code), "zpid": zpid, "url": url, "verification": verification},
                        )
                    save_json(
                        {
                            "last_checked": datetime.now().isoformat(),
                            "error": "verification_page",
                            "verification": verification,
                            "url": url,
                        },
                        out_path,
                    )
                    errors.append(f"zillow property {zpid}: verification page")
                    # Stop early for this ZIP to reduce additional suspicious traffic.
                    break

            next_data = _extract_next_data_json(getattr(driver, "page_source", "") or "")
            if not isinstance(next_data, dict):
                blocked += 1
                if getattr(args, "debug_snapshots", False):
                    save_page_diagnostics(
                        driver,
                        args.diagnostics_dir,
                        f"zillow_{zip_code}_property_{zpid}_missing_next_data",
                        extra={"zip_code": str(zip_code), "zpid": zpid, "url": url},
                    )
                save_json(
                    {
                        "last_checked": datetime.now().isoformat(),
                        "error": "missing_next_data",
                        "url": url,
                        "verification": _detail_verification_state(driver),
                    },
                    out_path,
                )
                errors.append(f"zillow property {zpid}: missing __NEXT_DATA__ (possible verification page)")
                continue

            if not _is_zillow_property_page_payload(next_data):
                # Avoid treating non-property payloads (or verification pages) as valid snapshots.
                blocked += 1
                if getattr(args, "debug_snapshots", False):
                    save_page_diagnostics(
                        driver,
                        args.diagnostics_dir,
                        f"zillow_{zip_code}_property_{zpid}_missing_component_props",
                        extra={"zip_code": str(zip_code), "zpid": zpid, "url": url, "next_data_keys": sorted(list(next_data.keys()))},
                    )
                save_json(
                    {
                        "last_checked": datetime.now().isoformat(),
                        "error": "unexpected_next_data_shape",
                        "url": url,
                        "verification": _detail_verification_state(driver),
                    },
                    out_path,
                )
                errors.append(f"zillow property {zpid}: unexpected __NEXT_DATA__ shape (possible verification page)")
                continue
            page_props = (next_data.get("props") or {}).get("pageProps") or {}
            component_props = (page_props.get("componentProps") or {}) if isinstance(page_props, dict) else {}
            cache_value = component_props.get("gdpClientCache")
            if isinstance(cache_value, str):
                try:
                    component_props["gdpClientCache"] = json.loads(cache_value)
                except json.JSONDecodeError:
                    pass

            next_data["last_checked"] = datetime.now().isoformat()
            next_data["source_url"] = url
            save_json(next_data, out_path)
            saved += 1
        except Exception as exc:
            save_json({"last_checked": datetime.now().isoformat(), "error": str(exc), "url": url}, out_path)
            errors.append(f"zillow property {zpid}: {type(exc).__name__}: {exc}")

    return {
        "enabled": True,
        "attempted": attempted,
        "saved": saved,
        "skipped": skipped,
        "blocked": blocked,
        "errors": errors[:10],
    }


def _load_provider(provider_name):
    if provider_name == "zillow":
        import re_analyzer.scrapers.zillow_search_scraper as zillow_search
        zillow_search.get_selenium_driver = scraping_utility.get_selenium_driver
        return ZillowListingProvider(zillow_search)
    if provider_name == "redfin":
        return RedfinListingProvider()
    if provider_name == "realtor":
        return RealtorListingProvider()
    raise ValueError(f"Unsupported provider: {provider_name}")


def _set_chrome_path(chrome_path):
    if not chrome_path:
        return
    scraping_utility.CHROME_BINARY_EXECUTABLE_PATH = str(Path(chrome_path).expanduser().resolve())


def _set_chromedriver_path(chromedriver_path):
    if not chromedriver_path:
        return
    path = Path(chromedriver_path).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"--chromedriver-path does not exist: {path}")
    scraping_utility.CHROMEDRIVER_EXECUTABLE_PATH = str(path)


def _set_chrome_user_data_dir(chrome_user_data_dir):
    if not chrome_user_data_dir:
        return
    user_data_dir = Path(chrome_user_data_dir).expanduser().resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    scraping_utility.CHROME_USER_DATA_DIR = str(user_data_dir)
    scraping_utility.local_path_exists = True


def _set_chrome_profile_directory(chrome_profile_directory):
    if chrome_profile_directory:
        scraping_utility.CHROME_PROFILE_DIRECTORY = str(chrome_profile_directory).strip() or None


def _clear_chrome_profile_cache(chrome_user_data_dir):
    """
    Remove disposable browser cache artifacts from an isolated scraper profile.

    This intentionally preserves cookies, local storage, and saved browser state
    so a human-solved session remains usable. It only clears transient cache
    directories that can leave stale page/service-worker behavior behind.
    """
    if not chrome_user_data_dir:
        return []
    root = Path(chrome_user_data_dir).expanduser().resolve()
    if not root.exists():
        return []

    candidates = {root / relative_path for relative_path in CHROME_CACHE_RELATIVE_PATHS}
    for profile_dir in root.glob("Profile *"):
        if not profile_dir.is_dir():
            continue
        for cache_name in CHROME_PROFILE_CACHE_NAMES:
            candidates.add(profile_dir / cache_name)

    removed = []
    for path in sorted(candidates):
        try:
            if path.is_dir():
                shutil.rmtree(path)
                removed.append(str(path))
            elif path.is_file():
                path.unlink()
                removed.append(str(path))
        except OSError as exc:
            print(f"Could not clear Chrome cache path {path}: {exc}")
    if removed:
        print(f"Cleared {len(removed)} Chrome cache paths under {root}")
    return removed


def _set_chromedriver_startup_lock_mode(mode):
    if not mode:
        return
    scraping_utility.CHROMEDRIVER_STARTUP_LOCK_MODE = mode


def _set_chromedriver_user_multi_procs(enabled):
    scraping_utility.CHROMEDRIVER_USER_MULTI_PROCS = bool(enabled)


def _zip_last_checked_epoch(provider_name: str, zip_code: str) -> float:
    """Return seconds-since-epoch of last check, or 0.0 for never-scraped (sorts first)."""
    metadata = _load_provider_zip_metadata(provider_name, zip_code)
    if not metadata:
        return 0.0
    checked_str = (
        metadata.get("last_checked")
        or metadata.get("last_successful_fetch")
        or metadata.get("legacy_last_checked")
    )
    if not checked_str:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(checked_str))
        return dt.timestamp()
    except Exception:
        return 0.0


def _bounded_zip_codes(args, provider):
    if args.zip_code:
        return [str(zip_code).strip() for zip_code in args.zip_code if str(zip_code).strip()]
    florida_zip_codes = [str(zip_code) for zip_code in scraping_utility.load_search_zip_codes()]
    if provider.source_name == "zillow":
        cached_zip_codes = {str(zip_code) for zip_code in provider.cached_query_state_data.keys()}
        zip_codes = [zip_code for zip_code in florida_zip_codes if zip_code in cached_zip_codes]
        zip_codes.extend(sorted(cached_zip_codes.difference(zip_codes)))
    else:
        zip_codes = florida_zip_codes
    zip_codes = [zip_code for zip_code in zip_codes if str(zip_code).isdigit()]
    if args.start_after_zip:
        start_after_zip = str(args.start_after_zip).strip()
        zip_codes_set = set(zip_codes)
        if start_after_zip in zip_codes_set:
            zip_codes = [z for z in zip_codes if z > start_after_zip]
        else:
            zip_codes = [z for z in zip_codes if z > start_after_zip]
    if args.respect_cooldown:
        zip_codes = [
            zip_code for zip_code in zip_codes
            if _should_process_zip_code(
                provider.source_name,
                zip_code,
                args.cooldown_hours,
                require_saved=getattr(args, "save", False),
            )
        ]
    zip_codes, eligibility_summary = filter_zip_codes_for_scrape(
        zip_codes,
        filter_name=args.zip_eligibility_filter,
        min_residential_or_other_ratio=args.min_residential_or_other_ratio,
        auto_build=getattr(args, "zip_eligibility_auto_build", True),
    )
    args.zip_eligibility_summary = eligibility_summary
    if eligibility_summary.get("skipped_count"):
        print(
            f"ZIP eligibility filter skipped {eligibility_summary['skipped_count']} of "
            f"{eligibility_summary['input_count']} ZIPs: {eligibility_summary.get('reason_counts')}"
        )
    elif args.zip_eligibility_filter != "none" and not eligibility_summary.get("applied"):
        print(
            "ZIP eligibility filter did not run; cached HUD-USPS eligibility file is missing. "
            "Run re_analyzer.scrapers.zip_eligibility with a HUD API token to build it "
            "(env: HUD_USPS_CROSSWALK_TOKEN / HUD_USER_API_TOKEN / HUD_API_TOKEN)."
        )
    if args.shuffle:
        random.shuffle(zip_codes)
    else:
        # Oldest/never-scraped first so each run prioritises the most stale data.
        zip_codes = sorted(zip_codes, key=lambda z: _zip_last_checked_epoch(provider.source_name, z))
    if args.max_zip_codes and args.max_zip_codes > 0:
        return zip_codes[: args.max_zip_codes]
    return zip_codes


def _should_process_zip_code(provider_name, zip_code, cooldown_hours, require_saved=False):
    support_status = _load_zip_support_status(provider_name, zip_code)
    if _support_status_is_fresh_skip(support_status, cooldown_hours):
        return False

    metadata = _load_provider_zip_metadata(provider_name, zip_code)
    if not metadata:
        return True
    if require_saved:
        checked_at = metadata.get("last_saved") or metadata.get("legacy_last_checked")
        if not checked_at and metadata.get("last_status") in {"empty", "error", "aborted", "unsupported"}:
            checked_at = metadata.get("last_checked") or metadata.get("last_attempted")
    else:
        checked_at = metadata.get("last_checked") or metadata.get("last_successful_fetch") or metadata.get("legacy_last_checked")
    return not is_within_cooldown_period(
        checked_at,
        timedelta(hours=max(0, cooldown_hours)),
    )


def _zip_support_registry_path():
    return os.path.join(DATA_PATH, "Fetched", "zip_provider_support.json")


def _load_zip_support_registry():
    path = _zip_support_registry_path()
    return load_json(path) if os.path.exists(path) else {}


def _load_zip_support_status(provider_name, zip_code):
    registry = _load_zip_support_registry()
    zip_code = str(zip_code)
    by_provider = registry.get("by_provider") or {}
    by_zip = registry.get("by_zip") or {}
    return {
        "provider": (by_provider.get(provider_name) or {}).get(zip_code) or {},
        "zip": by_zip.get(zip_code) or {},
    }


def _support_status_is_fresh_skip(support_status, cooldown_hours):
    provider_status = support_status.get("provider") or {}
    zip_status = support_status.get("zip") or {}
    if provider_status.get("status") == "unsupported" and is_within_cooldown_period(
        provider_status.get("checked_at"),
        timedelta(hours=max(0, cooldown_hours)),
    ):
        return True
    if provider_status.get("status") == "empty" and is_within_cooldown_period(
        provider_status.get("checked_at"),
        timedelta(hours=max(0, cooldown_hours)),
    ):
        return True
    if zip_status.get("all_provider_status") == "unsupported" and is_within_cooldown_period(
        zip_status.get("checked_at"),
        timedelta(hours=max(0, cooldown_hours)),
    ):
        return True
    return False


def _record_zip_support_status(provider_name, zip_code, status, checked_at, warnings=None, errors=None):
    path = _zip_support_registry_path()
    ensure_directory_exists(os.path.dirname(path))
    lock_path = f"{path}.lock"
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            _record_zip_support_status_locked(provider_name, zip_code, status, checked_at, warnings=warnings, errors=errors)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _record_zip_support_status_locked(provider_name, zip_code, status, checked_at, warnings=None, errors=None):
    support_status = "supported" if status in {"ok", "warning"} else status
    if support_status == "zip_mismatch":
        support_status = "unsupported"
    if support_status not in {"supported", "unsupported", "empty", "error", "aborted"}:
        support_status = "unknown"

    path = _zip_support_registry_path()
    registry = _load_zip_support_registry()
    by_provider = registry.setdefault("by_provider", {})
    by_zip = registry.setdefault("by_zip", {})
    provider_map = by_provider.setdefault(provider_name, {})
    zip_code = str(zip_code)
    messages = list(errors or []) or list(warnings or [])

    provider_map[zip_code] = {
        "status": support_status,
        "checked_at": checked_at,
        "last_message": messages[0] if messages else "",
    }

    provider_statuses = {
        provider: (by_provider.get(provider) or {}).get(zip_code, {}).get("status")
        for provider in KNOWN_PROVIDERS
    }
    known_statuses = [value for value in provider_statuses.values() if value]
    all_provider_status = "unknown"
    if len(known_statuses) == len(KNOWN_PROVIDERS) and all(value == "unsupported" for value in known_statuses):
        all_provider_status = "unsupported"
    elif any(value == "supported" for value in known_statuses):
        all_provider_status = "supported"

    by_zip[zip_code] = {
        "checked_at": checked_at,
        "all_provider_status": all_provider_status,
        "provider_statuses": provider_statuses,
    }

    registry["updated_at"] = checked_at
    ensure_directory_exists(os.path.dirname(path))
    save_json(registry, path)


def _metadata_path_for_provider(provider_name, zip_code):
    return os.path.join(DATA_PATH, "Fetched", provider_name, "Metadata", f"{zip_code}_metadata.json")


def _legacy_zillow_metadata_path(zip_code):
    return os.path.join(SEARCH_LISTINGS_METADATA_PATH, f"{zip_code}_metadata.json")


def _load_provider_zip_metadata(provider_name, zip_code):
    sidecar_path = _metadata_path_for_provider(provider_name, zip_code)
    metadata = load_json(sidecar_path) if os.path.exists(sidecar_path) else {}
    if provider_name == "zillow":
        legacy_path = _legacy_zillow_metadata_path(zip_code)
        if os.path.exists(legacy_path):
            legacy_metadata = load_json(legacy_path)
            metadata.setdefault("legacy_last_checked", legacy_metadata.get("last_checked"))
            metadata.setdefault("legacy_active_count", len(legacy_metadata.get("active_zpids") or []))
            metadata.setdefault("legacy_total_known_count", len(legacy_metadata.get("zpids") or []))
    return metadata


def _save_provider_results(provider, zip_code, raw_listings, canonical_listings):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    provider_root = os.path.join(DATA_PATH, "Fetched", provider.source_name)
    metadata_dir = os.path.join(provider_root, "Metadata")
    zip_dir = os.path.join(provider_root, str(zip_code))
    ensure_directory_exists(metadata_dir)
    ensure_directory_exists(zip_dir)

    raw_path = os.path.join(zip_dir, f"listings_{timestamp}.json")
    canonical_path = os.path.join(zip_dir, f"canonical_listings_{timestamp}.json")
    with open(raw_path, "w", encoding="utf-8") as file:
        json.dump(raw_listings, file, indent=4, default=str)
    with open(canonical_path, "w", encoding="utf-8") as file:
        json.dump([asdict(listing) for listing in canonical_listings], file, indent=4, default=str)

    injection_manifest = write_zip_injection_manifest(
        provider.source_name,
        str(zip_code),
        canonical_path,
        raw_path=raw_path,
        metadata_path=_metadata_path_for_provider(provider.source_name, zip_code),
    )
    injection_manifest_path = injection_manifest.get("manifest_path")
    injection_ready_count = int((injection_manifest.get("summary") or {}).get("canonical_listing_count") or 0)

    if provider.source_name == "zillow":
        provider.zillow_search.maybe_save_current_search_results(str(zip_code), raw_listings)
        return {
            "saved_payload": True,
            "raw_path": raw_path,
            "canonical_path": canonical_path,
            "injection_manifest_path": injection_manifest_path,
            "injection_ready_count": injection_ready_count,
            "legacy_metadata_path": _legacy_zillow_metadata_path(zip_code),
        }

    metadata = {
        "last_checked": datetime.now().isoformat(),
        "active_source_property_ids": [
            listing.source_property_id for listing in canonical_listings if listing.source_property_id
        ],
        "active_canonical_property_ids": [
            listing.canonical_property_id for listing in canonical_listings if listing.canonical_property_id
        ],
        "raw_count": len(raw_listings),
        "canonical_count": len(canonical_listings),
        "raw_path": raw_path,
        "canonical_path": canonical_path,
    }
    save_json(metadata, os.path.join(metadata_dir, f"{zip_code}_metadata.json"))
    return {
        "saved_payload": True,
        "raw_path": raw_path,
        "canonical_path": canonical_path,
        "injection_manifest_path": injection_manifest_path,
        "injection_ready_count": injection_ready_count,
    }


def _record_provider_zip_metadata(
    provider,
    zip_code,
    args,
    raw_listings,
    canonical_listings,
    status,
    run_started_at,
    run_completed_at,
    pages_requested,
    pages_processed,
    empty_pages,
    warnings,
    errors,
    diagnostics,
    requests,
    save_result=None,
):
    metadata_path = _metadata_path_for_provider(provider.source_name, zip_code)
    existing_metadata = load_json(metadata_path) if os.path.exists(metadata_path) else {}
    existing_history = existing_metadata.get("recent_runs") or []
    now = run_completed_at
    zip_counts = Counter(str(listing.zip_code or "unknown") for listing in canonical_listings)
    requested_zip = str(zip_code)
    matching_count = zip_counts.get(requested_zip, 0)
    canonical_count = len(canonical_listings)
    mismatch_count = max(0, canonical_count - matching_count)
    dominant_zip, dominant_zip_count = (zip_counts.most_common(1)[0] if zip_counts else ("", 0))
    active_source_ids = [
        listing.source_property_id for listing in canonical_listings if listing.source_property_id
    ]
    active_canonical_ids = [
        listing.canonical_property_id for listing in canonical_listings if listing.canonical_property_id
    ]
    save_result = save_result or {}
    metadata = {
        **existing_metadata,
        "provider": provider.source_name,
        "zip_code": requested_zip,
        "last_checked": now,
        "last_attempted": now,
        "last_status": status,
        "last_successful_fetch": now if raw_listings else existing_metadata.get("last_successful_fetch"),
        "last_empty": now if not raw_listings else existing_metadata.get("last_empty"),
        "last_saved": now if save_result.get("saved_payload") else existing_metadata.get("last_saved"),
        "has_saved_payload": bool(save_result.get("saved_payload") or existing_metadata.get("has_saved_payload")),
        "last_run": {
            "started_at": run_started_at,
            "completed_at": run_completed_at,
            "dry_run": not args.save,
            "status": status,
            "raw_count": len(raw_listings),
            "canonical_count": canonical_count,
            "pages_requested": pages_requested,
            "pages_processed": pages_processed,
            "empty_pages": empty_pages,
            "warning_count": len(warnings),
            "error_count": len(errors),
            "diagnostic_count": len(diagnostics),
            "all_pages": bool(args.all_pages),
            "max_discovered_pages_per_zip": args.max_discovered_pages_per_zip,
            "redfin_rental_estimates": bool(getattr(args, "redfin_rental_estimates", False)),
            "redfin_rental_estimate_limit_per_zip": int(getattr(args, "redfin_rental_estimate_limit_per_zip", 0) or 0),
            "redfin_rental_estimate_delay_seconds": float(getattr(args, "redfin_rental_estimate_delay_seconds", 0.25) or 0),
            "zillow_property_details": ({
                "enabled": bool(getattr(args, "zillow_property_details", False)),
                "attempted": int((save_result.get("zillow_property_details") or {}).get("attempted") or 0),
                "saved": int((save_result.get("zillow_property_details") or {}).get("saved") or 0),
                "skipped": int((save_result.get("zillow_property_details") or {}).get("skipped") or 0),
                "error_count": len((save_result.get("zillow_property_details") or {}).get("errors") or []),
            } if provider.source_name == "zillow" else None),
        },
        "active_source_property_ids": active_source_ids,
        "active_canonical_property_ids": active_canonical_ids,
        "raw_count": len(raw_listings),
        "canonical_count": canonical_count,
        "requested_zip_match_count": matching_count,
        "requested_zip_mismatch_count": mismatch_count,
        "requested_zip_match_ratio": round(matching_count / canonical_count, 4) if canonical_count else None,
        "dominant_listing_zip_code": dominant_zip,
        "dominant_listing_zip_count": dominant_zip_count,
        "listing_zip_code_counts": dict(zip_counts.most_common(12)),
        "warnings": warnings[:12],
        "errors": errors[:12],
        "diagnostic_reasons": [item.get("reason") for item in diagnostics if isinstance(item, dict)][-8:],
        "request_cache": _summarize_requests(requests),
        "saved_paths": {
            key: value for key, value in {
                "raw_path": save_result.get("raw_path"),
                "canonical_path": save_result.get("canonical_path"),
                "injection_manifest_path": save_result.get("injection_manifest_path"),
                "legacy_metadata_path": save_result.get("legacy_metadata_path"),
                "property_details_dir": (
                    os.path.join(PROPERTY_DETAILS_PATH, str(zip_code))
                    if provider.source_name == "zillow" and (save_result.get("zillow_property_details") or {}).get("attempted")
                    else None
                ),
            }.items() if value
        },
    }
    metadata["recent_runs"] = ([
        {
            "checked_at": now,
            "dry_run": not args.save,
            "status": status,
            "raw_count": len(raw_listings),
            "canonical_count": canonical_count,
            "pages_processed": pages_processed,
            "warning_count": len(warnings),
            "error_count": len(errors),
            "requested_zip_match_ratio": metadata["requested_zip_match_ratio"],
        }
    ] + existing_history)[:10]
    ensure_directory_exists(os.path.dirname(metadata_path))
    save_json(metadata, metadata_path)
    _record_zip_support_status(
        provider.source_name,
        zip_code,
        status,
        now,
        warnings=warnings,
        errors=errors,
    )


def _summarize_requests(requests):
    summarized = []
    for request in requests[-12:]:
        if not isinstance(request, dict):
            continue
        summarized.append({
            "page": request.get("page"),
            "zip_code": request.get("zip_code"),
            "url": request.get("url"),
            "method": request.get("method"),
            "status": request.get("status"),
            "ok": request.get("ok"),
            "content_type": request.get("content_type"),
            "region": request.get("region"),
            "raw_count": request.get("raw_count"),
            "total_pages": request.get("total_pages"),
            "redfin_rental_estimates": request.get("redfin_rental_estimates"),
        })
    return summarized


def _effective_zip_delay(args, provider_name: str) -> float:
    """Return the inter-ZIP delay to use, respecting the realtor-specific override."""
    if provider_name == "realtor":
        realtor_delay = float(getattr(args, "realtor_zip_delay_seconds", 0.0) or 0.0)
        if realtor_delay > 0:
            return realtor_delay
    return float(args.zip_delay_seconds)


def run_scrape(args):
    _set_chrome_path(args.chrome_path)
    _set_chromedriver_path(getattr(args, "chromedriver_path", ""))
    _set_chrome_user_data_dir(args.chrome_user_data_dir)
    _set_chrome_profile_directory(args.chrome_profile_directory)
    if args.clear_profile_cache:
        _clear_chrome_profile_cache(scraping_utility.CHROME_USER_DATA_DIR)
    _set_chromedriver_startup_lock_mode(args.driver_startup_lock)
    _set_chromedriver_user_multi_procs(args.driver_user_multi_procs)
    provider = _load_provider(args.provider)
    zip_codes = _bounded_zip_codes(args, provider)
    page_limit = max(1, args.max_pages)
    discovered_page_cap = max(1, args.max_discovered_pages_per_zip)
    errors = []
    pages_requested = 0
    pages_processed = 0
    zip_codes_processed = 0
    raw_seen = 0
    canonical_seen = 0
    saved_zip_codes = 0
    empty_pages = 0
    warnings = []
    diagnostics = []
    abort_run = False
    provider_backoff = ProviderBackoffState()
    realtor_zips_fetched = 0

    if not zip_codes:
        raise ValueError("No ZIP codes selected for this run.")

    window_rect = _window_rect_from_args(args)
    with scraping_utility.get_selenium_driver(
        "about:blank",
        ignore_detection=args.ignore_detection,
        random_profile=args.random_profile,
        clean_profile=args.clean_profile,
        window_rect=window_rect,
        enforce_window_rect=args.enforce_window_rect,
    ) as driver:
        for zip_index, zip_code in enumerate(zip_codes):
            _maybe_wait_for_backoff(provider_backoff, provider.source_name)
            zip_started_at = datetime.now().isoformat()
            zip_pages_requested_start = pages_requested
            zip_pages_processed_start = pages_processed
            zip_empty_pages_start = empty_pages
            zip_warnings_start = len(warnings)
            zip_errors_start = len(errors)
            zip_diagnostics_start = len(diagnostics)
            zip_requests = []
            raw_for_zip = []
            canonical_for_zip = []
            zip_redfin_rental_estimate_attempts = 0
            zip_aborted = False
            try:
                if hasattr(provider, "prepare_session"):
                    provider.prepare_session(driver, zip_code)
            except UnsupportedProviderRegionError as exc:
                warning = f"{provider.source_name} ZIP {zip_code}: unsupported provider region: {exc}"
                warnings.append(warning)
                print(warning)
                diagnostics.append(_maybe_save_diagnostics(driver, args, provider.source_name, zip_code, "unsupported_region", {
                    "warning": warning,
                }))
                _record_provider_zip_metadata(
                    provider=provider,
                    zip_code=zip_code,
                    args=args,
                    raw_listings=raw_for_zip,
                    canonical_listings=canonical_for_zip,
                    status="unsupported",
                    run_started_at=zip_started_at,
                    run_completed_at=datetime.now().isoformat(),
                    pages_requested=pages_requested - zip_pages_requested_start,
                    pages_processed=pages_processed - zip_pages_processed_start,
                    empty_pages=empty_pages - zip_empty_pages_start,
                    warnings=warnings[zip_warnings_start:],
                    errors=[],
                    diagnostics=diagnostics[zip_diagnostics_start:],
                    requests=zip_requests,
                    save_result={},
                )
                zip_codes_processed += 1
                if zip_index < len(zip_codes) - 1:
                    random_delay(_effective_zip_delay(args, provider.source_name), _effective_zip_delay(args, provider.source_name) + args.max_delay_seconds)
                continue
            except ProviderBlockedError as exc:
                reference_id = getattr(exc, "reference_id", "") or ""
                ref_note = f" reference_id={reference_id}" if reference_id else ""
                warning = (
                    f"{provider.source_name} ZIP {zip_code}: blocked during prepare_session "
                    f"reason={getattr(exc, 'reason', '')} status={getattr(exc, 'status', None)}{ref_note}"
                )
                warnings.append(warning)
                print(warning, flush=True)
                diagnostics.append(_maybe_save_diagnostics(driver, args, provider.source_name, zip_code, "prepare_session_blocked", {
                    "warning": warning,
                    "blocked": {
                        "provider": getattr(exc, "provider", provider.source_name),
                        "url": getattr(exc, "url", ""),
                        "status": getattr(exc, "status", None),
                        "reason": getattr(exc, "reason", ""),
                        "reference_id": reference_id,
                        "snippet": (getattr(exc, "snippet", "") or "")[:800],
                    },
                    "challenge": detect_challenge(driver),
                }))
                zip_aborted = True
                _apply_block_backoff(args, provider_backoff, provider.source_name, reason=getattr(exc, "reason", "blocked_prepare_session"))
                if args.stop_on_challenge or _check_max_consecutive_blocks(args, provider_backoff, provider.source_name):
                    abort_run = True
                _record_provider_zip_metadata(
                    provider=provider,
                    zip_code=zip_code,
                    args=args,
                    raw_listings=raw_for_zip,
                    canonical_listings=canonical_for_zip,
                    status="aborted",
                    run_started_at=zip_started_at,
                    run_completed_at=datetime.now().isoformat(),
                    pages_requested=pages_requested - zip_pages_requested_start,
                    pages_processed=pages_processed - zip_pages_processed_start,
                    empty_pages=empty_pages - zip_empty_pages_start,
                    warnings=warnings[zip_warnings_start:],
                    errors=[],
                    diagnostics=diagnostics[zip_diagnostics_start:],
                    requests=zip_requests,
                    save_result={},
                )
                zip_codes_processed += 1
                if abort_run:
                    break
                if zip_index < len(zip_codes) - 1:
                    random_delay(_effective_zip_delay(args, provider.source_name), _effective_zip_delay(args, provider.source_name) + args.max_delay_seconds)
                continue
            except Exception as exc:
                message = f"{provider.source_name} ZIP {zip_code}: prepare_session {type(exc).__name__}: {exc}"
                errors.append(message)
                print(message)
                diagnostics.append(_maybe_save_diagnostics(driver, args, provider.source_name, zip_code, "prepare_session_error", {
                    "error": message,
                }))
                _record_provider_zip_metadata(
                    provider=provider,
                    zip_code=zip_code,
                    args=args,
                    raw_listings=raw_for_zip,
                    canonical_listings=canonical_for_zip,
                    status="error",
                    run_started_at=zip_started_at,
                    run_completed_at=datetime.now().isoformat(),
                    pages_requested=pages_requested - zip_pages_requested_start,
                    pages_processed=pages_processed - zip_pages_processed_start,
                    empty_pages=empty_pages - zip_empty_pages_start,
                    warnings=warnings[zip_warnings_start:],
                    errors=errors[zip_errors_start:],
                    diagnostics=diagnostics[zip_diagnostics_start:],
                    requests=zip_requests,
                    save_result={},
                )
                zip_codes_processed += 1
                if _is_dead_session(exc):
                    print(
                        f"[session-dead] {type(exc).__name__} during prepare_session — "
                        "Chrome window is gone; aborting batch. Restart the run to resume from the next ZIP.",
                        flush=True,
                    )
                    abort_run = True
                    break
                if args.stop_on_error:
                    raise
                if zip_index < len(zip_codes) - 1:
                    random_delay(_effective_zip_delay(args, provider.source_name), _effective_zip_delay(args, provider.source_name) + args.max_delay_seconds)
                continue
            _warm_after_navigation(args, provider.source_name, zip_code, is_first_zip=(zip_index == 0), provider=provider)
            initial_challenge = detect_challenge(driver)
            if initial_challenge["is_challenge"]:
                warning = f"{provider.source_name} ZIP {zip_code}: challenge detected before API fetch"
                warnings.append(warning)
                print(warning)
                diagnostics.append(_maybe_save_diagnostics(driver, args, provider.source_name, zip_code, "pre_fetch", {
                    "challenge": initial_challenge,
                }))
                initial_challenge = wait_for_manual_challenge(driver, args.manual_challenge_wait_seconds)
                if not initial_challenge["is_challenge"]:
                    print(f"{provider.source_name} ZIP {zip_code}: challenge cleared")
                    diagnostics.append(_maybe_save_diagnostics(driver, args, provider.source_name, zip_code, "pre_fetch_cleared", {
                        "challenge": initial_challenge,
                    }))
                    if hasattr(provider, "prepare_session"):
                        provider.prepare_session(driver, zip_code)
                if initial_challenge["is_challenge"]:
                    guidance = (
                        f"{provider.source_name} ZIP {zip_code}: challenge still present; aborting ZIP to avoid "
                        "repeated blocked requests. "
                        "Set --manual-challenge-wait-seconds to pause for human resolution if auto-interaction fails."
                    )
                    warnings.append(guidance)
                    print(guidance)
                    diagnostics.append(_maybe_save_diagnostics(driver, args, provider.source_name, zip_code, "pre_fetch_aborted", {
                        "challenge": initial_challenge,
                        "manual_challenge_wait_seconds": float(args.manual_challenge_wait_seconds or 0),
                    }))
                    zip_aborted = True
                    _apply_block_backoff(args, provider_backoff, provider.source_name, reason="challenge_pre_fetch")
                    if args.stop_on_challenge or _check_max_consecutive_blocks(args, provider_backoff, provider.source_name):
                        abort_run = True
                        break
            if zip_aborted:
                # Metadata is recorded at the end of the ZIP scope.
                pass
            else:
                page = 1
                total_pages = 1 if args.all_pages else page_limit
                while page <= total_pages:
                    try:
                        page_data = None
                        raw_listings = []
                        pages_requested += 1
                        for attempt in range(args.empty_page_retries + 1):
                            try:
                                page_data = provider.fetch_search_page(driver, zip_code, page)
                            except ProviderBlockedError as exc:
                                reference_id = str(getattr(exc, "reference_id", "") or "").strip()
                                ref_note = f" reference_id={reference_id}" if reference_id else ""
                                warning = (
                                    f"{provider.source_name} ZIP {zip_code} page {page}: blocked request; "
                                    f"status={getattr(exc, 'status', None)} reason={getattr(exc, 'reason', '')}{ref_note}"
                                )
                                warnings.append(warning)
                                print(warning, flush=True)
                                diagnostics.append(_maybe_save_diagnostics(driver, args, provider.source_name, zip_code, f"page_{page}_blocked", {
                                    "warning": warning,
                                    "blocked": {
                                        "provider": getattr(exc, "provider", provider.source_name),
                                        "url": getattr(exc, "url", ""),
                                        "status": getattr(exc, "status", None),
                                        "reason": getattr(exc, "reason", ""),
                                        "reference_id": reference_id,
                                        "snippet": (getattr(exc, "snippet", "") or "")[:800],
                                    },
                                    "challenge": detect_challenge(driver),
                                }))
                                zip_aborted = True
                                _apply_block_backoff(args, provider_backoff, provider.source_name, reason=getattr(exc, "reason", "blocked"))
                                if args.stop_on_challenge or _check_max_consecutive_blocks(args, provider_backoff, provider.source_name):
                                    abort_run = True
                                break
                            raw_listings = list(provider.raw_listings_from_page(page_data))
                            if raw_listings or attempt >= args.empty_page_retries:
                                break
                            challenge = detect_challenge(driver)
                            if challenge["is_challenge"]:
                                warning = f"{provider.source_name} ZIP {zip_code} page {page}: challenge detected after empty response"
                                warnings.append(warning)
                                print(warning)
                                diagnostics.append(_maybe_save_diagnostics(driver, args, provider.source_name, zip_code, f"page_{page}_challenge", {
                                    "attempt": attempt + 1,
                                    "challenge": challenge,
                                }))
                                challenge = wait_for_manual_challenge(driver, args.manual_challenge_wait_seconds)
                                if not challenge["is_challenge"]:
                                    print(f"{provider.source_name} ZIP {zip_code} page {page}: challenge cleared")
                                    diagnostics.append(_maybe_save_diagnostics(driver, args, provider.source_name, zip_code, f"page_{page}_cleared", {
                                        "attempt": attempt + 1,
                                        "challenge": challenge,
                                    }))
                                    if hasattr(provider, "prepare_session"):
                                        provider.prepare_session(driver, zip_code)
                                    continue
                                blocked = (
                                    f"{provider.source_name} ZIP {zip_code} page {page}: "
                                    "challenge still present; aborting ZIP to avoid repeated blocked requests. "
                                    "Set --manual-challenge-wait-seconds to pause for human resolution if auto-interaction fails."
                                )
                                warnings.append(blocked)
                                print(blocked)
                                diagnostics.append(_maybe_save_diagnostics(driver, args, provider.source_name, zip_code, f"page_{page}_aborted", {
                                    "attempt": attempt + 1,
                                    "challenge": challenge,
                                    "manual_challenge_wait_seconds": float(args.manual_challenge_wait_seconds or 0),
                                }))
                                zip_aborted = True
                                _apply_block_backoff(args, provider_backoff, provider.source_name, reason="challenge_after_empty")
                                if args.stop_on_challenge or _check_max_consecutive_blocks(args, provider_backoff, provider.source_name):
                                    abort_run = True
                                break
                            # Empty results without an explicit challenge can still reflect soft blocking.
                            # Back off slightly if we keep seeing empties after retries.
                            print(
                                f"{provider.source_name} ZIP {zip_code} page {page}: "
                                f"empty response, retrying after a short delay"
                            )
                            random_delay(_effective_zip_delay(args, provider.source_name), _effective_zip_delay(args, provider.source_name) + args.max_delay_seconds)
                        if zip_aborted or abort_run:
                            break
                        redfin_rental_estimate_result = {}
                        if (
                            raw_listings
                            and provider.source_name == "redfin"
                            and getattr(args, "redfin_rental_estimates", False)
                            and hasattr(provider, "enrich_rental_estimates")
                        ):
                            enrichment_limit = max(0, int(getattr(args, "redfin_rental_estimate_limit_per_zip", 0) or 0))
                            page_limit_remaining = 0
                            if enrichment_limit:
                                page_limit_remaining = max(0, enrichment_limit - zip_redfin_rental_estimate_attempts)
                            if not enrichment_limit or page_limit_remaining:
                                redfin_rental_estimate_result = provider.enrich_rental_estimates(
                                    driver,
                                    raw_listings,
                                    limit=page_limit_remaining,
                                    delay_seconds=getattr(args, "redfin_rental_estimate_delay_seconds", 0.25),
                                )
                                zip_redfin_rental_estimate_attempts += redfin_rental_estimate_result.get("attempted", 0)
                                if redfin_rental_estimate_result.get("attempted"):
                                    print(
                                        f"{provider.source_name} ZIP {zip_code} page {page}: "
                                        "rental estimates "
                                        f"{redfin_rental_estimate_result.get('succeeded', 0)}/"
                                        f"{redfin_rental_estimate_result.get('attempted', 0)} found"
                                    )
                                if (
                                    redfin_rental_estimate_result.get("error_count")
                                    and not redfin_rental_estimate_result.get("succeeded")
                                ):
                                    first_error = (redfin_rental_estimate_result.get("errors") or [{}])[0].get("error", "")
                                    warning = (
                                        f"{provider.source_name} ZIP {zip_code} page {page}: "
                                        f"rental estimate enrichment failed for "
                                        f"{redfin_rental_estimate_result.get('error_count')} listings"
                                    )
                                    if first_error:
                                        warning = f"{warning}: {first_error}"
                                    warnings.append(warning)
                                    print(warning)
                        canonical_listings = [provider.canonicalize_listing(raw) for raw in raw_listings]
                        if not raw_listings:
                            empty_pages += 1
                            reached_pagination_end = bool(
                                args.all_pages
                                and args.stop_zip_on_empty_page
                                and raw_for_zip
                                and page > total_pages
                            )
                            if reached_pagination_end:
                                print(f"{provider.source_name} ZIP {zip_code} page {page}: reached empty discovered page; stopping ZIP")
                            else:
                                warnings.append(f"{provider.source_name} ZIP {zip_code} page {page}: empty page after retries")
                                diagnostics.append(_maybe_save_diagnostics(driver, args, provider.source_name, zip_code, f"page_{page}_empty", {
                                    "page_data_keys": list((page_data or {}).keys()) if isinstance(page_data, dict) else [],
                                    "request": (page_data or {}).get("_request") if isinstance(page_data, dict) else None,
                                }))
                            if args.all_pages and args.stop_zip_on_empty_page:
                                break
                        provider_total_pages = provider.total_pages(page_data) if hasattr(provider, "total_pages") else page_limit
                        if args.all_pages:
                            total_pages = min(max(1, provider_total_pages), discovered_page_cap)
                        else:
                            total_pages = min(page_limit, max(1, provider_total_pages))
                        if isinstance(page_data, dict):
                            request_snapshot = dict(page_data.get("_request") or {})
                        else:
                            request_snapshot = {}
                        request_snapshot.update({
                            "page": page,
                            "zip_code": str(zip_code),
                            "raw_count": len(raw_listings),
                            "total_pages": total_pages,
                        })
                        if redfin_rental_estimate_result:
                            request_snapshot["redfin_rental_estimates"] = redfin_rental_estimate_result
                        zip_requests.append(request_snapshot)
                        raw_for_zip.extend(raw_listings)
                        canonical_for_zip.extend(canonical_listings)
                        raw_seen += len(raw_listings)
                        canonical_seen += len(canonical_listings)
                        pages_processed += 1
                        print(
                            f"{provider.source_name} ZIP {zip_code} page {page}/{total_pages}: "
                            f"{len(raw_listings)} raw, {len(canonical_listings)} canonical"
                        )
                        for listing in canonical_listings[: args.sample_size]:
                            print(json.dumps({
                                "canonical_property_id": listing.canonical_property_id,
                                "source_property_id": listing.source_property_id,
                                "address": listing.address,
                                "zip_code": listing.zip_code,
                                "price": listing.price,
                                "rent_estimate": listing.rent_estimate,
                            }, sort_keys=True))
                    except UnsupportedProviderRegionError as exc:
                        warning = f"{provider.source_name} ZIP {zip_code} page {page}: unsupported provider region: {exc}"
                        warnings.append(warning)
                        print(warning)
                        diagnostics.append(_maybe_save_diagnostics(driver, args, provider.source_name, zip_code, f"page_{page}_unsupported_region", {
                            "warning": warning,
                        }))
                        break
                    except Exception as exc:
                        message = f"{provider.source_name} ZIP {zip_code} page {page}: {type(exc).__name__}: {exc}"
                        errors.append(message)
                        print(message)
                        if _is_dead_session(exc):
                            print(
                                f"[session-dead] {type(exc).__name__} during page fetch — "
                                "Chrome window is gone; aborting batch. Restart the run to resume from the next ZIP.",
                                flush=True,
                            )
                            abort_run = True
                            break
                        if args.stop_on_challenge and "challenge detected" in str(exc).lower():
                            abort_run = True
                            break
                        if args.stop_on_error:
                            raise
                    random_delay(args.min_delay_seconds, args.max_delay_seconds)
                    if abort_run:
                        break
                    page += 1
            save_result = {}
            if args.save and raw_for_zip:
                save_result = _save_provider_results(provider, zip_code, raw_for_zip, canonical_for_zip) or {}
                saved_zip_codes += 1
            if provider.source_name == "zillow" and getattr(args, "zillow_property_details", False) and canonical_for_zip:
                details_result = _fetch_zillow_property_details_for_zip(driver, zip_code, canonical_for_zip, args)
                save_result = save_result or {}
                save_result["zillow_property_details"] = details_result
                if details_result.get("attempted"):
                    print(
                        f"zillow ZIP {zip_code}: property details "
                        f"{details_result.get('saved', 0)}/{details_result.get('attempted', 0)} saved"
                    )
                if details_result.get("errors"):
                    warnings.append(
                        f"zillow ZIP {zip_code}: property detail fetch had {len(details_result.get('errors') or [])} errors"
                    )
            zip_errors = errors[zip_errors_start:]
            zip_warnings = warnings[zip_warnings_start:]
            zip_diagnostics = diagnostics[zip_diagnostics_start:]
            _record_provider_zip_metadata(
                provider=provider,
                zip_code=zip_code,
                args=args,
                raw_listings=raw_for_zip,
                canonical_listings=canonical_for_zip,
                status=_zip_run_status(
                    raw_for_zip,
                    zip_errors,
                    abort_run or zip_aborted,
                    warnings=zip_warnings,
                    requested_zip=zip_code,
                    canonical_listings=canonical_for_zip,
                ),
                run_started_at=zip_started_at,
                run_completed_at=datetime.now().isoformat(),
                pages_requested=pages_requested - zip_pages_requested_start,
                pages_processed=pages_processed - zip_pages_processed_start,
                empty_pages=empty_pages - zip_empty_pages_start,
                warnings=zip_warnings,
                errors=zip_errors,
                diagnostics=zip_diagnostics,
                requests=zip_requests,
                save_result=save_result,
            )
            zip_codes_processed += 1
            if not zip_aborted and (pages_processed > zip_pages_processed_start):
                provider_backoff.block_count = 0
                provider_backoff.blocked_until_epoch = 0.0
                provider_backoff.last_reason = ""
            if not zip_aborted:
                # Signal to the control server that this ZIP completed so the resume
                # pointer only advances past ZIPs that finished, not ones mid-flight.
                print(f"[resume-checkpoint] {provider.source_name} ZIP {zip_code} completed", flush=True)
            if abort_run:
                break
            if provider.source_name == "realtor":
                if pages_requested > zip_pages_requested_start:
                    realtor_zips_fetched += 1
                realtor_zip_budget = int(getattr(args, "realtor_zip_budget", 0) or 0)
                if realtor_zip_budget > 0 and realtor_zips_fetched >= realtor_zip_budget:
                    print(
                        f"[realtor-budget] {realtor_zips_fetched}/{realtor_zip_budget} fetched ZIPs reached — "
                        "stopping session to reset fingerprint. Next run auto-resumes from the next ZIP.",
                        flush=True,
                    )
                    break
            if zip_index < len(zip_codes) - 1:
                random_delay(_effective_zip_delay(args, provider.source_name), _effective_zip_delay(args, provider.source_name) + args.max_delay_seconds)

    scraping_utility.kill_chrome_leaks()
    return ScrapeRunSummary(
        provider=provider.source_name,
        dry_run=not args.save,
        zip_codes_requested=len(zip_codes),
        zip_codes_processed=zip_codes_processed,
        zip_eligibility_filter=getattr(args, "zip_eligibility_summary", {}),
        pages_requested=pages_requested,
        pages_processed=pages_processed,
        raw_listings_seen=raw_seen,
        canonical_listings_seen=canonical_seen,
        saved_zip_codes=saved_zip_codes,
        empty_pages=empty_pages,
        warnings=warnings,
        errors=errors,
        diagnostics=diagnostics,
    )


def _maybe_save_diagnostics(driver, args, provider_name, zip_code, reason, extra=None):
    if not args.debug_snapshots:
        return {
            "saved": False,
            "provider": provider_name,
            "zip_code": str(zip_code),
            "reason": reason,
            "challenge": detect_challenge(driver),
        }
    return {
        "saved": True,
        "provider": provider_name,
        "zip_code": str(zip_code),
        "reason": reason,
        **save_page_diagnostics(
            driver,
            args.diagnostics_dir,
            f"{provider_name}_{zip_code}_{reason}",
            extra=extra,
        )
    }


def _zip_run_status(raw_listings, errors, abort_run, warnings=None, requested_zip=None, canonical_listings=None):
    if abort_run:
        return "aborted"
    if errors:
        return "error"
    if any("unsupported provider region" in str(warning).lower() for warning in (warnings or [])):
        return "unsupported"
    if canonical_listings and requested_zip:
        matching_count = sum(
            1 for listing in canonical_listings
            if str(getattr(listing, "zip_code", "") or "") == str(requested_zip)
        )
        if matching_count == 0:
            return "zip_mismatch"
    if raw_listings:
        return "ok"
    return "empty"


def _warm_after_navigation(args, provider_name, zip_code, is_first_zip=False, provider=None):
    # If the provider skipped the page navigation (reused an existing session),
    # skip the ZIP warmup — there is no page to settle. Still apply the one-time
    # session warmup on the first ZIP even when navigation was skipped, since the
    # driver may have just started.
    did_navigate = getattr(provider, "_did_navigate", True)
    seconds = max(0, float(args.zip_navigation_warmup_seconds or 0)) if did_navigate else 0.0
    if is_first_zip:
        seconds += max(0, float(args.session_warmup_seconds or 0))
    if seconds <= 0:
        return
    print(
        f"{provider_name} ZIP {zip_code}: warming loaded page for about {round(seconds, 1)}s "
        "before API fetch"
    )
    random_delay(max(0, seconds * 0.8), max(0.2, seconds * 1.2))


def _window_rect_from_args(args):
    values = [args.window_x, args.window_y, args.window_width, args.window_height]
    if any(value is None for value in values):
        return None
    return {
        "x": args.window_x,
        "y": args.window_y,
        "width": args.window_width,
        "height": args.window_height,
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Conservative bounded listing scraper runner.")
    parser.add_argument("--provider", choices=["zillow", "redfin", "realtor"], default="zillow")
    parser.add_argument(
        "--zillow-property-details",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Zillow-only: after collecting search results, visit each listing URL and persist the property page __NEXT_DATA__ payload.",
    )
    parser.add_argument(
        "--zillow-property-details-limit-per-zip",
        type=int,
        default=25,
        help="Zillow-only: cap on property detail pages to fetch per ZIP (0 = no cap).",
    )
    parser.add_argument(
        "--zillow-property-details-delay-seconds",
        type=float,
        default=2.0,
        help="Zillow-only: pacing delay between property detail navigations.",
    )
    parser.add_argument(
        "--zillow-property-details-cooldown-hours",
        type=float,
        default=36.0,
        help="Zillow-only: skip existing property detail snapshots inside this cooldown window unless --zillow-property-details-force is enabled.",
    )
    parser.add_argument(
        "--zillow-property-details-force",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Zillow-only: re-fetch property details even if a recent snapshot already exists.",
    )
    parser.add_argument("--zip-code", action="append", help="ZIP code to scrape. Can be provided multiple times.")
    parser.add_argument("--max-zip-codes", type=int, default=DEFAULT_ZIP_LIMIT, help="Maximum ZIPs to process. Use 0 to process every selected ZIP.")
    parser.add_argument("--start-after-zip", default="", help="Resume a state-wide run after this ZIP code.")
    parser.add_argument("--respect-cooldown", action=argparse.BooleanOptionalAction, default=False, help="Skip ZIPs with fresh saved search metadata.")
    parser.add_argument("--cooldown-hours", type=float, default=36.0)
    parser.add_argument("--zip-eligibility-filter", choices=["none", "hud_usps"], default="hud_usps", help="Skip ZIPs marked ineligible by cached HUD-USPS ZIP eligibility.")
    parser.add_argument(
        "--zip-eligibility-auto-build",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When HUD-USPS eligibility cache is missing, attempt to build it automatically if a HUD token is available in env vars.",
    )
    parser.add_argument("--min-residential-or-other-ratio", type=float, default=0.0)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_PAGE_LIMIT, help="Fixed page cap unless --all-pages is enabled.")
    parser.add_argument("--all-pages", action="store_true", help="Discover provider totalPages from the response and continue until that page count.")
    parser.add_argument("--max-discovered-pages-per-zip", type=int, default=20, help="Safety cap when --all-pages is enabled.")
    parser.add_argument("--stop-zip-on-empty-page", action=argparse.BooleanOptionalAction, default=True, help="In --all-pages mode, stop the current ZIP when a discovered page returns empty after retries.")
    parser.add_argument("--min-delay-seconds", type=float, default=3.0)
    parser.add_argument("--max-delay-seconds", type=float, default=6.0)
    parser.add_argument("--zip-delay-seconds", type=float, default=15.0)
    parser.add_argument("--realtor-zip-delay-seconds", type=float, default=0.0, help="Override --zip-delay-seconds for realtor only. 0 = use --zip-delay-seconds.")
    parser.add_argument("--realtor-zip-budget", type=int, default=0, help="Stop the realtor session after this many fetched ZIPs to reset the browser fingerprint. 0 = no limit.")
    parser.add_argument("--session-warmup-seconds", type=float, default=0, help="Extra first-ZIP pause after the browser page loads and before API requests.")
    parser.add_argument("--zip-navigation-warmup-seconds", type=float, default=0, help="Pause after each ZIP page loads and before provider API requests.")
    parser.add_argument("--redfin-rental-estimates", action=argparse.BooleanOptionalAction, default=False, help="From the active Redfin browser page, fetch Redfin's per-property rental estimate endpoint for each Redfin row.")
    parser.add_argument("--redfin-rental-estimate-limit-per-zip", type=int, default=0, help="Safety cap for Redfin rental estimate enrichment per ZIP. Use 0 for all Redfin rows.")
    parser.add_argument("--redfin-rental-estimate-delay-seconds", type=float, default=0.25, help="Small pacing delay between Redfin per-property rental estimate requests.")
    parser.add_argument("--sample-size", type=int, default=3)
    parser.add_argument("--empty-page-retries", type=int, default=1)
    parser.add_argument("--diagnostics-dir", default="re_analyzer/Data/ScraperDiagnostics")
    parser.add_argument("--debug-snapshots", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--stop-on-challenge", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--manual-challenge-wait-seconds", type=float, default=0)
    parser.add_argument(
        "--blocked-backoff-base-seconds",
        type=float,
        default=60.0,
        help="When a provider request appears blocked (captcha/interstitial/403/etc), sleep for this long (0 disables).",
    )
    parser.add_argument(
        "--blocked-backoff-max-seconds",
        type=float,
        default=600.0,
        help="Maximum sleep duration for repeated blocked events.",
    )
    parser.add_argument(
        "--blocked-backoff-multiplier",
        type=float,
        default=2.0,
        help="Exponential multiplier applied after each repeated blocked event in a single run.",
    )
    parser.add_argument(
        "--max-consecutive-blocks",
        type=int,
        default=0,
        help=(
            "Abort the run after this many consecutive block responses (0 = no limit). "
            "Useful for Realtor runs where repeated blocks indicate the session is burned. "
            "The next run will auto-resume from the next unprocessed ZIP."
        ),
    )
    parser.add_argument(
        "--blocked-backoff-jitter-seconds",
        type=float,
        default=3.0,
        help="Random jitter added to each blocked backoff delay.",
    )
    parser.add_argument("--chrome-path", default="")
    parser.add_argument("--chromedriver-path", default="", help="Optional pinned chromedriver binary path. Overrides UC caching.")
    parser.add_argument("--chrome-user-data-dir", default="", help="Optional Chrome user-data directory override.")
    parser.add_argument("--chrome-profile-directory", default="Default", help="Chrome profile directory inside --chrome-user-data-dir. Default keeps isolated runs to one stable profile.")
    parser.add_argument("--clear-profile-cache", action="store_true", help="Clear disposable cache directories from the selected Chrome profile before launch.")
    parser.add_argument("--window-x", type=int, default=None)
    parser.add_argument("--window-y", type=int, default=None)
    parser.add_argument("--window-width", type=int, default=None)
    parser.add_argument("--window-height", type=int, default=None)
    parser.add_argument("--enforce-window-rect", action="store_true", help="Call set_window_rect after Chrome starts. Slower, but useful if Chrome ignores launch geometry.")
    parser.add_argument("--driver-startup-lock", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--driver-user-multi-procs", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--save", action="store_true", help="Persist provider-native results. Default is dry-run.")
    parser.add_argument("--shuffle", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--random-profile", action="store_true")
    parser.add_argument("--clean-profile", action="store_true")
    parser.add_argument("--ignore-detection", action=argparse.BooleanOptionalAction, default=True, help="Bypass the legacy driver.get() challenge waiter so the runner can handle diagnostics and manual challenge waits.")
    return parser.parse_args()


def main():
    args = parse_args()
    summary = run_scrape(args)
    print("SCRAPE_RUN_SUMMARY")
    print(json.dumps(asdict(summary), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
