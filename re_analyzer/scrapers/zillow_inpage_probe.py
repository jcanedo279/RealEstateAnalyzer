import argparse
import json
from copy import deepcopy
from pathlib import Path

from re_analyzer.scrapers import scraping_utility
from re_analyzer.scrapers.page_diagnostics import (
    detect_challenge,
    save_page_diagnostics,
    wait_for_manual_challenge,
)
from re_analyzer.scrapers.zillow_search_scraper import (
    initialize_driver_session_for_zip_code,
    query_state_data,
    scrape_listings_in_zip_code_for_page,
)


def _resolve_path(value: str) -> str:
    return str(Path(value).expanduser().resolve())


def _set_chrome_overrides(args):
    if args.chrome_path:
        scraping_utility.CHROME_BINARY_EXECUTABLE_PATH = _resolve_path(args.chrome_path)
    if args.chrome_user_data_dir:
        user_data_dir = Path(args.chrome_user_data_dir).expanduser().resolve()
        user_data_dir.mkdir(parents=True, exist_ok=True)
        scraping_utility.CHROME_USER_DATA_DIR = str(user_data_dir)
        scraping_utility.local_path_exists = True
    if args.chrome_profile_directory:
        scraping_utility.CHROME_PROFILE_DIRECTORY = str(args.chrome_profile_directory).strip() or None


def _window_rect_from_args(args):
    values = [args.window_x, args.window_y, args.window_width, args.window_height]
    if any(value is None for value in values):
        return None
    return {
        "x": int(args.window_x),
        "y": int(args.window_y),
        "width": int(args.window_width),
        "height": int(args.window_height),
    }


def _load_query_state(zip_code: str) -> dict:
    key = str(zip_code)
    state = query_state_data.get(key)
    if not state and key.isdigit():
        state = query_state_data.get(int(key))
    if not isinstance(state, dict):
        raise ValueError(f"No cached query state found for ZIP {zip_code}.")
    return deepcopy(state)


def run_probe(args):
    _set_chrome_overrides(args)
    zip_code = str(args.zip_code).strip()
    page = int(args.page or 1)
    if page <= 0:
        raise ValueError("--page must be >= 1")

    query_state = _load_query_state(zip_code)
    window_rect = _window_rect_from_args(args)
    diagnostics_dir = str(args.diagnostics_dir or "").strip()
    snapshot = bool(args.snapshot)

    with scraping_utility.get_selenium_driver(
        "about:blank",
        ignore_detection=True,
        random_profile=False,
        clean_profile=False,
        window_rect=window_rect,
        enforce_window_rect=bool(args.enforce_window_rect),
    ) as driver:
        initialize_driver_session_for_zip_code(driver, zip_code)
        before = detect_challenge(driver)
        if snapshot and diagnostics_dir:
            save_page_diagnostics(
                driver,
                diagnostics_dir,
                f"zillow_probe_{zip_code}_before",
                extra={
                    "zip_code": zip_code,
                    "page": page,
                    "before": before,
                },
            )

        if before.get("is_challenge") and (args.manual_challenge_wait_seconds or 0) > 0:
            wait_for_manual_challenge(driver, float(args.manual_challenge_wait_seconds), poll_seconds=2.0)
            before = detect_challenge(driver)

        data = scrape_listings_in_zip_code_for_page(
            driver,
            query_state,
            page,
            use_page_navigation=bool(getattr(args, "use_page_navigation", False)),
        )
        after = detect_challenge(driver)

        request_meta = {}
        if isinstance(data, dict):
            request_meta = data.get("_request") or {}

        list_results = []
        try:
            list_results = (((data or {}).get("cat1") or {}).get("searchResults") or {}).get("listResults") or []
        except Exception:
            list_results = []

        if snapshot and diagnostics_dir:
            save_page_diagnostics(
                driver,
                diagnostics_dir,
                f"zillow_probe_{zip_code}_after",
                extra={
                    "zip_code": zip_code,
                    "page": page,
                    "before": before,
                    "after": after,
                    "request": request_meta,
                    "list_results_count": len(list_results),
                    "error": (data or {}).get("_error") if isinstance(data, dict) else None,
                },
            )

        report = {
            "zip_code": zip_code,
            "page": page,
            "before": before,
            "after": after,
            "request": request_meta,
            "request_error": (data or {}).get("_request_error") if isinstance(data, dict) else None,
            "list_results_count": len(list_results),
            "data_keys": sorted(list(data.keys())) if isinstance(data, dict) else [],
            "error": (data or {}).get("_error") if isinstance(data, dict) else None,
        }
        print("ZILLOW_INPAGE_PROBE_REPORT")
        print(json.dumps(report, indent=2, sort_keys=True))


def parse_args():
    parser = argparse.ArgumentParser(description="Probe in-page Zillow async fetch behavior and challenge visibility.")
    parser.add_argument("--zip-code", required=True)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--manual-challenge-wait-seconds", type=float, default=0)
    parser.add_argument(
        "--use-page-navigation",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Navigate to `/{page}_p/` and parse `__NEXT_DATA__` instead of running an in-page async fetch.",
    )
    parser.add_argument("--diagnostics-dir", default="re_analyzer/Data/ScraperDiagnostics/Probe")
    parser.add_argument("--snapshot", action="store_true", help="Save before/after page snapshots to diagnostics-dir.")
    parser.add_argument("--chrome-path", default="")
    parser.add_argument("--chrome-user-data-dir", default="")
    parser.add_argument("--chrome-profile-directory", default="Default")
    parser.add_argument("--window-x", type=int, default=None)
    parser.add_argument("--window-y", type=int, default=None)
    parser.add_argument("--window-width", type=int, default=None)
    parser.add_argument("--window-height", type=int, default=None)
    parser.add_argument("--enforce-window-rect", action="store_true")
    return parser.parse_args()


def main():
    run_probe(parse_args())


if __name__ == "__main__":
    main()
