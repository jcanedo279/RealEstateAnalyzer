import argparse
import json
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from urllib.parse import urlencode

from re_analyzer.scrapers import scraping_utility
from re_analyzer.scrapers.page_diagnostics import detect_challenge, save_page_diagnostics
from re_analyzer.scrapers.zillow_search_scraper import (
    initialize_driver_session_for_zip_code,
    query_state_data,
    scrape_listings_in_zip_code_for_page,
)


DEFAULT_ZIP_CODES = ("32404", "32801", "33301", "33602", "34102", "33040", "33176")


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


def _safe_list_results_count(data: dict) -> int:
    try:
        list_results = (((data or {}).get("cat1") or {}).get("searchResults") or {}).get("listResults") or []
        return len(list_results)
    except Exception:
        return 0


def _markdown_escape(text: str) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def _render_markdown_table(rows: Sequence[dict], headers: Sequence[str]) -> str:
    headers = list(headers)
    widths = {header: len(header) for header in headers}
    for row in rows:
        for header in headers:
            widths[header] = max(widths[header], len(str(row.get(header, ""))))

    def fmt_row(values):
        parts = []
        for header, value in zip(headers, values):
            parts.append(str(value).ljust(widths[header]))
        return "| " + " | ".join(parts) + " |"

    out = []
    out.append(fmt_row(headers))
    out.append("| " + " | ".join("-" * widths[h] for h in headers) + " |")
    for row in rows:
        out.append(fmt_row([row.get(h, "") for h in headers]))
    return "\n".join(out)


def _short_patterns(challenge: dict) -> str:
    patterns = challenge.get("matched_patterns") or []
    if not patterns:
        return ""
    return ",".join(patterns[:4]) + ("…" if len(patterns) > 4 else "")


def _diagnostics_prefix(zip_code: str, page: int, label: str) -> str:
    safe_zip = "".join(ch for ch in str(zip_code) if ch.isdigit())[:5] or "zip"
    return f"zillow_probe_matrix_{safe_zip}_p{page}_{label}"


def run_matrix(args):
    _set_chrome_overrides(args)

    zip_codes = []
    if args.zip_code:
        zip_codes.extend([str(value).strip() for value in args.zip_code if str(value).strip()])
    if args.zip_codes:
        zip_codes.extend([value.strip() for value in str(args.zip_codes).split(",") if value.strip()])
    if not zip_codes:
        zip_codes = list(DEFAULT_ZIP_CODES)

    pages = []
    if args.pages:
        pages = [int(value) for value in str(args.pages).split(",") if str(value).strip()]
    if not pages:
        pages = [1, 2]
    pages = [page for page in pages if page >= 1]

    diagnostics_dir = str(args.diagnostics_dir or "").strip()
    snapshot = bool(args.snapshot) and bool(diagnostics_dir)
    warmup_seconds = float(args.warmup_seconds or 0)
    zip_delay_seconds = float(args.zip_delay_seconds or 0)

    window_rect = _window_rect_from_args(args)

    started_at = datetime.now(timezone.utc).isoformat()
    results: List[dict] = []

    with scraping_utility.get_selenium_driver(
        "about:blank",
        ignore_detection=True,
        random_profile=False,
        clean_profile=False,
        window_rect=window_rect,
        enforce_window_rect=bool(args.enforce_window_rect),
    ) as driver:
        for index, zip_code in enumerate(zip_codes):
            zip_code = str(zip_code).strip()
            initialize_driver_session_for_zip_code(driver, zip_code)
            before = detect_challenge(driver)

            if snapshot:
                save_page_diagnostics(
                    driver,
                    diagnostics_dir,
                    _diagnostics_prefix(zip_code, 0, "before"),
                    extra={
                        "zip_code": zip_code,
                        "before": before,
                    },
                )

            if warmup_seconds > 0:
                time.sleep(max(0.0, warmup_seconds))

            for page in pages:
                query_state = _load_query_state(zip_code)
                data = scrape_listings_in_zip_code_for_page(
                    driver,
                    query_state,
                    page,
                    use_page_navigation=bool(getattr(args, "use_page_navigation", False)),
                )
                after = detect_challenge(driver)

                request_meta = {}
                request_error = None
                error = None
                list_results_count = 0
                data_keys: List[str] = []
                if isinstance(data, dict):
                    request_meta = data.get("_request") or {}
                    request_error = data.get("_request_error")
                    error = data.get("_error")
                    data_keys = sorted(list(data.keys()))
                    list_results_count = _safe_list_results_count(data)

                item = {
                    "zip_code": zip_code,
                    "page": page,
                    "request_source": request_meta.get("source"),
                    "request_status": request_meta.get("status"),
                    "request_ok": request_meta.get("ok"),
                    "content_type": request_meta.get("content_type"),
                    "elapsed_ms": request_meta.get("elapsed_ms"),
                    "list_results_count": list_results_count,
                    "request_error": request_error,
                    "error": error,
                    "before_is_challenge": bool(before.get("is_challenge")),
                    "before_is_soft_challenge": bool(before.get("is_soft_challenge")),
                    "before_patterns": before.get("matched_patterns") or [],
                    "after_is_challenge": bool(after.get("is_challenge")),
                    "after_is_soft_challenge": bool(after.get("is_soft_challenge")),
                    "after_patterns": after.get("matched_patterns") or [],
                    "data_keys": data_keys[:40],
                }
                results.append(item)

                if snapshot:
                    save_page_diagnostics(
                        driver,
                        diagnostics_dir,
                        _diagnostics_prefix(zip_code, page, "after"),
                        extra=item,
                    )

            if zip_delay_seconds > 0 and index < (len(zip_codes) - 1):
                time.sleep(max(0.0, zip_delay_seconds))

    payload = {
        "started_at": started_at,
        "zip_codes": zip_codes,
        "pages": pages,
        "warmup_seconds": warmup_seconds,
        "zip_delay_seconds": zip_delay_seconds,
        "use_page_navigation": bool(getattr(args, "use_page_navigation", False)),
        "results": results,
    }

    out_json = str(args.out_json or "").strip()
    if out_json:
        out_path = Path(out_json).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    out_md = str(args.out_md or "").strip()
    if out_md:
        out_path = Path(out_md).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(_render_markdown_report(payload), encoding="utf-8")

    print("ZILLOW_PROBE_MATRIX_REPORT")
    print(json.dumps(payload, indent=2, sort_keys=True))


def _render_markdown_report(payload: dict) -> str:
    started_at = payload.get("started_at") or ""
    zip_codes = payload.get("zip_codes") or []
    pages = payload.get("pages") or []
    warmup_seconds = payload.get("warmup_seconds")
    zip_delay_seconds = payload.get("zip_delay_seconds")
    results = payload.get("results") or []

    rows = []
    for item in results:
        rows.append({
            "zip": item.get("zip_code", ""),
            "page": item.get("page", ""),
            "status": item.get("request_status", ""),
            "ok": item.get("request_ok", ""),
            "type": _markdown_escape(item.get("content_type", "") or ""),
            "ms": item.get("elapsed_ms", ""),
            "list": item.get("list_results_count", ""),
            "fallback": "yes" if item.get("request_error") else "",
            "before": ("hard" if item.get("before_is_challenge") else ("soft" if item.get("before_is_soft_challenge") else "")),
            "after": ("hard" if item.get("after_is_challenge") else ("soft" if item.get("after_is_soft_challenge") else "")),
            "patterns": _markdown_escape(_short_patterns({"matched_patterns": item.get("after_patterns") or []})),
            "error": _markdown_escape(item.get("error") or ""),
        })

    md = []
    md.append("# Zillow probe matrix findings")
    md.append("")
    md.append(f"- Run UTC: `{started_at}`")
    md.append(f"- ZIPs: `{', '.join(zip_codes)}`")
    md.append(f"- Pages: `{', '.join(str(p) for p in pages)}`")
    md.append(f"- Warmup seconds: `{warmup_seconds}`")
    md.append(f"- Delay between ZIPs: `{zip_delay_seconds}`")
    md.append(f"- Page navigation: `{bool(payload.get('use_page_navigation'))}`")
    md.append("")
    md.append("## Results")
    md.append("")
    md.append(_render_markdown_table(
        rows,
        headers=["zip", "page", "status", "ok", "type", "ms", "list", "fallback", "before", "after", "patterns", "error"],
    ))
    md.append("")
    md.append("## Interpretation notes")
    md.append("")
    md.append("- `status/type` describe the in-page `fetch` to `/async-create-search-page-state`.")
    md.append("- `list` is `cat1.searchResults.listResults` count from the data payload that the scraper consumed.")
    md.append("- `fallback=yes` means the async request errored but page 1 used the server-rendered `__NEXT_DATA__` payload instead.")
    md.append("- `before/after=soft` means PerimeterX markers were present in the HTML, but the page still contained usable listing content.")
    md.append("")
    return "\n".join(md)


def parse_args():
    parser = argparse.ArgumentParser(description="Run a small matrix of Zillow in-page probe checks across ZIPs/pages.")
    parser.add_argument("--zip-code", action="append", help="ZIP code to probe (repeatable).")
    parser.add_argument("--zip-codes", default="", help="Comma-separated ZIP codes to probe.")
    parser.add_argument("--pages", default="1,2", help="Comma-separated page numbers to probe (default: 1,2).")
    parser.add_argument("--warmup-seconds", type=float, default=3.0, help="Seconds to wait after navigation before probes.")
    parser.add_argument("--zip-delay-seconds", type=float, default=1.0, help="Seconds to wait between ZIP navigations.")
    parser.add_argument(
        "--use-page-navigation",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Navigate to `/{page}_p/` and parse `__NEXT_DATA__` instead of running an in-page async fetch.",
    )
    parser.add_argument("--diagnostics-dir", default="re_analyzer/Data/ScraperDiagnostics/ProbeMatrix")
    parser.add_argument("--snapshot", action="store_true", help="Save diagnostics snapshots to diagnostics-dir.")
    parser.add_argument("--out-json", default="re_analyzer/Data/ScraperDiagnostics/ProbeMatrix/report.json")
    parser.add_argument("--out-md", default="re_analyzer/Data/ScraperDiagnostics/ProbeMatrix/report.md")
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
    run_matrix(parse_args())


if __name__ == "__main__":
    main()
