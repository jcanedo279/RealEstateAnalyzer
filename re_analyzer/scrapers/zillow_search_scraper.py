import os
import random as rd
import json
import re
import time
from datetime import datetime, timedelta

from re_analyzer.utility.utility import (
    DATA_PATH, SEARCH_LISTINGS_METADATA_PATH, SEARCH_LISTINGS_DATA_PATH,
    ensure_directory_exists, load_json, is_within_cooldown_period, save_json, batch_generator, backoff_strategy
)
from re_analyzer.scrapers.scraping_utility import get_selenium_driver, kill_chrome_leaks, load_search_metadata, send_sms_alert
from re_analyzer.scrapers.page_diagnostics import detect_challenge, _attempt_auto_press_and_hold


# JS that returns True only when the PX captcha button is in the DOM AND has a
# real rendered height (>= 30px), meaning the PX SDK has finished setting it up.
_PX_READY_JS = """
return (function() {
    if (document.getElementById('px-captcha-modal')) return true;
    var wrapper = document.getElementById('px-captcha-wrapper');
    if (wrapper) {
        var wr = wrapper.getBoundingClientRect();
        if (wr.width >= 50 && wr.height >= 30) return true;
    }
    var el = document.getElementById('px-captcha')
           || document.querySelector('.px-captcha-container');
    if (!el) return false;
    var r = el.getBoundingClientRect();
    return r.width >= 50 && r.height >= 30;
})();
"""


def _wait_for_px_element(driver, timeout=10.0, interval=0.5):
    """
    Poll until #px-captcha / .px-captcha-container is in the DOM and fully
    sized (PX SDK has had time to set up the iframes).  Returns True if found.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if driver.execute_script(_PX_READY_JS):
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _maybe_resolve_px_challenge(driver, max_attempts=2, debug=False):
    """
    Attempt CDP-based resolution of a PerimeterX press-and-hold challenge.

    Polls for the PX button to be fully rendered before attempting (the SDK
    injects and sizes its UI asynchronously — calling too soon means the
    element either isn't in the DOM yet or has zero height).

    detect_challenge is only used to bail out on confirmed non-PX challenges
    (Cloudflare, reCAPTCHA) that require human-in-the-loop.
    """
    # Bail on confirmed non-PX challenges before we do anything else.
    try:
        challenge = detect_challenge(driver)
        if challenge.get("is_challenge"):
            matched = challenge.get("matched_patterns") or []
            is_px = "px_captcha" in matched or "press_and_hold" in matched
            if not is_px:
                return {"skipped": True, "reason": f"non_px_challenge: {matched}"}
    except Exception:
        pass

    if not _wait_for_px_element(driver):
        return {"skipped": True, "reason": "no_px_element"}

    result = {}
    for attempt in range(1, max_attempts + 1):
        result = _attempt_auto_press_and_hold(driver, debug=debug)
        if not result.get("attempted"):
            return {"skipped": True, "reason": result.get("reason", "no_element")}
        if result.get("cleared"):
            print(
                f"[px] Press-and-hold cleared on attempt {attempt}/{max_attempts} "
                f"(selector={result.get('selector')} hold={result.get('hold_seconds', 0):.1f}s)",
                flush=True,
            )
            return {"cleared": True, "attempts": attempt, **result}
        print(
            f"[px] Press-and-hold attempt {attempt}/{max_attempts} not cleared "
            f"(reason={result.get('reason', '')} selector={result.get('selector', '')})",
            flush=True,
        )
        if attempt < max_attempts:
            time.sleep(rd.uniform(1.5, 2.5))

    send_sms_alert(
        f"[zillow-px] press-and-hold not cleared after {max_attempts} attempt(s): "
        f"reason={result.get('reason', '')} selector={result.get('selector', '')}"
    )
    return {"cleared": False, "attempts": max_attempts, **result}


###########
## SETUP ##
###########

def should_process_zip_code(zip_code):
    """
        Determines if a zip code should be processed based on cooldown period.
    """
    metadata_path = os.path.join(SEARCH_LISTINGS_METADATA_PATH, f"{zip_code}_metadata.json")
    if not os.path.exists(metadata_path):
        return True  # Process if no metadata exists
    metadata = load_json(metadata_path)
    return not is_within_cooldown_period(metadata.get('last_checked'), COOLDOWN_PERIOD)


# Global configuration.
COOLDOWN_PERIOD = timedelta(hours=36)
QUERY_STATE_DATA_PATH = os.path.join(DATA_PATH, 'query_state_data.json')
zip_code_to_seen_zpid_tuple = set()

# Ensure necessary directories exist.
ensure_directory_exists(SEARCH_LISTINGS_METADATA_PATH)

# Load initial data.
zip_code_to_zpids = load_search_metadata()
all_known_zpids = {zpid for zpids in zip_code_to_zpids.values() for zpid in zpids}
query_state_data = load_json(QUERY_STATE_DATA_PATH) if os.path.exists(QUERY_STATE_DATA_PATH) else {}
zip_code_to_query_state_data = {
    zip_code: data for zip_code, data in query_state_data.items()
    if should_process_zip_code(zip_code)
}


#####################
## SEARCH SCRAPERS ##
#####################

def scrape_listings(batch_size=10):
    """
        Main function to orchestrate the scraping process in batches.
    """
    zip_code_to_query_state_data_items = rd.sample(
        list(zip_code_to_query_state_data.items()),
        len(zip_code_to_query_state_data)
    )

    if not zip_code_to_query_state_data_items:
        print("No query state data found or all zip codes are within cooldown period.")
        return

    zip_code_ind = 0
    for batch in batch_generator(zip_code_to_query_state_data_items, batch_size):
        with get_selenium_driver("about:blank", ignore_detection=True) as driver:
            initialize_driver_session_for_zip_code(driver, batch[0][0])
            for zip_code, query_state_data in batch:
                scrape_zip_code(driver, zip_code, zip_code_ind, query_state_data)
                zip_code_ind += 1
        kill_chrome_leaks()


def scrape_zip_code(driver, zip_code, zip_code_ind, query_state_data):
    """
        Scrapes listings for a single zip code.
    """
    search_results = []
    page = 1
    total_pages = 1
    attempt = 1

    while page <= total_pages:
        print(f'Searching for listings in zip code: {zip_code} # [{zip_code_ind+1} | {len(zip_code_to_query_state_data.keys())}]  page [{page} | {total_pages}]', end='         \r')
        search_data = scrape_listings_in_zip_code_for_page(driver, query_state_data, page)
        if not search_data:
            return
        if isinstance(search_data, dict) and search_data.get("_error"):
            request_meta = search_data.get("_request") or {}
            print(
                f"\nZillow request failed for ZIP {zip_code} page {page}: "
                f"error={search_data.get('_error')} status={request_meta.get('status')} ok={request_meta.get('ok')} "
                f"content_type={request_meta.get('content_type')} body_prefix={request_meta.get('body_prefix')!r}"
            )
            backoff_strategy(attempt)
            attempt += 1
            continue
        while not process_search_results(zip_code, search_results, search_data):
            backoff_strategy(attempt)
            attempt += 1
            search_data = scrape_listings_in_zip_code_for_page(driver, query_state_data, page)
            if isinstance(search_data, dict) and search_data.get("_error"):
                request_meta = search_data.get("_request") or {}
                print(
                    f"\nZillow request failed for ZIP {zip_code} page {page}: "
                    f"error={search_data.get('_error')} status={request_meta.get('status')} ok={request_meta.get('ok')} "
                    f"content_type={request_meta.get('content_type')} body_prefix={request_meta.get('body_prefix')!r}"
                )
                backoff_strategy(attempt)
                attempt += 1
                continue
        if total_pages == 1:
            total_pages = min(int(((search_data.get("cat1") or {}).get("searchList") or {}).get("totalPages") or 1), 20)
        page += 1
    print(f"\nFound {len(search_results)} in {zip_code}")
    if not search_results:
        return
    maybe_save_current_search_results(zip_code, search_results)

def scrape_listings_in_zip_code_for_page(driver, query_state_data, page, *, use_page_navigation=False):
    """
        Fetches listings for a specific page within a zip code's search results.
    """
    if use_page_navigation:
        zip_code = _zip_code_from_query_state(query_state_data)
        if zip_code:
            page_url = _zip_page_url(zip_code, page)
            try:
                if (driver.current_url or "").rstrip("/") != page_url.rstrip("/"):
                    driver.get(page_url)
            except Exception:
                pass
            _maybe_resolve_px_challenge(driver, debug=True)
            page_data = extract_initial_search_data_from_page(driver)
            if isinstance(page_data, dict):
                page_data.setdefault(
                    "_request",
                    {
                        "source": "__NEXT_DATA__",
                        "method": "GET",
                        "url": (getattr(driver, "current_url", "") or ""),
                    },
                )
                return page_data

    query_state_data["pagination"] = {"currentPage": page}
    js_code = """
    (async () => {
        const queryState = arguments[0];
        const done = arguments[arguments.length - 1];
        const url = "https://www.zillow.com/async-create-search-page-state";
        const startedAt = Date.now();
        try {
            const response = await fetch(url, {
                method: "PUT",
                credentials: "include",
                headers: {
                    "content-type": "application/json"
                },
                body: JSON.stringify({
                    "searchQueryState": queryState,
                    "wants": {
                        "cat1": ["listResults", "mapResults"],
                        "cat2": ["total"]
                    },
                    "requestId": 5,
                    "isDebugRequest": false
                })
            });
            const contentType = response.headers.get("content-type") || "";
            const status = response.status;
            const ok = response.ok;
            const bodyText = await response.text();
            let data = null;
            let parseError = "";
            try {
                data = bodyText ? JSON.parse(bodyText) : null;
            } catch (e) {
                parseError = String(e || "json_parse_failed");
                data = null;
            }
            const meta = {
                url,
                method: "PUT",
                status,
                ok,
                content_type: contentType,
                elapsed_ms: Date.now() - startedAt,
                body_prefix: (bodyText || "").slice(0, 160),
            };
            if (data && typeof data === "object") {
                data._request = meta;
                done(data);
                return;
            }
            done({ _request: meta, _error: parseError || "non_json_response" });
        } catch (error) {
            done({
                _request: {
                    url,
                    method: "PUT",
                    elapsed_ms: Date.now() - startedAt,
                    error: String(error || "fetch_failed"),
                },
                _error: "fetch_failed",
            });
        }
    })();
    """

    data = driver.execute_async_script(js_code, query_state_data)
    if page == 1:
        if not data:
            data = extract_initial_search_data_from_page(driver)
        elif isinstance(data, dict) and data.get("_error"):
            fallback = extract_initial_search_data_from_page(driver)
            if isinstance(fallback, dict):
                # Preserve request diagnostics from the failed async call, but prefer
                # the server-rendered __NEXT_DATA__ payload for page one.
                fallback["_request"] = data.get("_request") or {}
                fallback["_request_error"] = data.get("_error")
                data = fallback
    return data


def _zip_code_from_query_state(query_state_data):
    try:
        users_search_term = (query_state_data or {}).get("usersSearchTerm")
    except Exception:
        users_search_term = ""
    zip_code = str(users_search_term or "").strip()
    return zip_code if zip_code.isdigit() else ""


def _zip_page_url(zip_code, page):
    zip_code = str(zip_code).strip()
    page = int(page or 1)
    base_url = f"https://www.zillow.com/homes/{zip_code}_rb/"
    if page <= 1:
        return base_url
    return f"{base_url}{page}_p/"


def extract_initial_search_data_from_page(driver):
    """
        Falls back to Zillow's server-rendered initial search state.

        Some fresh browser profiles receive a visible search page but the async
        create-search-page-state request returns no payload. For page one, the
        page's __NEXT_DATA__ script already contains the same cat1 search shape.
    """
    try:
        page_source = driver.page_source or ""
    except Exception:
        return None
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(?P<payload>.*?)</script>',
        page_source,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        next_data = json.loads(match.group("payload"))
    except json.JSONDecodeError:
        return None
    search_state = (
        ((next_data.get("props") or {}).get("pageProps") or {}).get("searchPageState")
    )
    if not isinstance(search_state, dict):
        return None
    list_results = (((search_state.get("cat1") or {}).get("searchResults") or {}).get("listResults") or [])
    return search_state if list_results else None


#######################
## SEARCH PROCESSING ##
#######################

def process_search_results(zip_code, search_results, search_data):
    """
        Processes search results, filtering and updating global sets.
    """

    try:
        current_search_results = (
            (((search_data or {}).get("cat1") or {}).get("searchResults") or {}).get("listResults") or []
        )
    except Exception:
        return False
    current_search_zpids = tuple(search_result['zpid'] for search_result in current_search_results)
    # We should back off if we have seen this ordered tuple of zpids (likely from faked server data).
    if current_search_zpids in zip_code_to_seen_zpid_tuple:
        return False
    # Filter out visited zpids and results from other zip codes.
    current_search_results = [search_result for search_result in current_search_results if search_result['zpid'] not in all_known_zpids and int(search_result.get('addressZipcode', -1)) == int(zip_code)]
    current_search_zpids = tuple(search_result['zpid'] for search_result in current_search_results)
    # Its posible we have real data but have either processed or should not process the results, we should not back off.
    if not current_search_results:
        return True
    zip_code_to_seen_zpid_tuple.add(current_search_zpids)
    search_results.extend(current_search_results)
    all_known_zpids.update(current_search_zpids)
    return True

def maybe_save_current_search_results(zip_code, search_results):
    """
        Saves search results and updates metadata for a zip code.
    """
    search_results_zpids = [new_search_result['zpid'] for new_search_result in search_results]
    zip_code_to_zpids[zip_code].update(search_results_zpids)

    search_metadata = {
        'zpids': list(zip_code_to_zpids[zip_code]),
        'active_zpids': list(search_results_zpids),
        'last_checked': datetime.now().isoformat()
    }
    save_json(search_metadata, os.path.join(SEARCH_LISTINGS_METADATA_PATH, f"{zip_code}_metadata.json"))

    zip_code_path = os.path.join(SEARCH_LISTINGS_DATA_PATH, zip_code)
    # We save even if new_search_results is empty to ensure the metadata exists to skip over this zip code.
    ensure_directory_exists(zip_code_path)
    formatted_current_time = datetime.now().strftime("%Y-%m-%d_%H-%M")
    save_json(search_results, os.path.join(zip_code_path, f"listings_{formatted_current_time}.json"))


#################
## SEARCH UTIL ##
#################

def initialize_driver_session_for_zip_code(driver, zip_code):
    """
        Initializes a web session for the given zip code.
    """
    base_url = f"https://www.zillow.com/homes/{zip_code}_rb/"
    driver.get(base_url)
    _maybe_resolve_px_challenge(driver, debug=True)
    return



if __name__ == "__main__":
    scrape_listings()
