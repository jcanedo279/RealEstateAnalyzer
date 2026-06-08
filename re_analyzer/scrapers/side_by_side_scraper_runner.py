import argparse
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from re_analyzer.scrapers import scraping_utility
from re_analyzer.scrapers.scraper_runner import _should_process_zip_code
from re_analyzer.scrapers.source_reconciler import DEFAULT_PROVIDERS as RECONCILER_PROVIDERS
from re_analyzer.scrapers.source_reconciler import capture_reconciliation_debug_screenshots, reconcile_sources, save_reconciliation_report
from re_analyzer.scrapers.zip_eligibility import filter_zip_codes_for_scrape


DEFAULT_PROVIDERS = ["zillow", "redfin", "realtor"]


def _provider_profile_dir(root_dir, provider, suffix):
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(suffix or "session")).strip("_") or "session"
    path = Path(root_dir).expanduser().resolve() / f"{provider}_{safe_suffix}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fresh_provider_profile_dir(root_dir, provider, suffix):
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(suffix or "session")).strip("_") or "session"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(root_dir).expanduser().resolve() / f"{provider}_{safe_suffix}_{ts}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _window_rect_for_provider(args, index, count):
    if not args.tile_windows:
        return None
    total_gap = max(0, count - 1) * args.tile_gap
    width = max(320, int((args.tile_screen_width - total_gap) / count))
    return {
        "x": int(args.tile_screen_x + index * (width + args.tile_gap)),
        "y": int(args.tile_screen_y),
        "width": width,
        "height": int(args.tile_screen_height),
    }


def _selected_zip_codes_for_aligned_refresh(args, providers):
    if not args.florida_refresh:
        return []

    zip_codes = [str(zip_code) for zip_code in scraping_utility.load_search_zip_codes()]
    if args.start_after_zip:
        start_after_zip = str(args.start_after_zip).strip()
        if start_after_zip in zip_codes:
            zip_codes = zip_codes[zip_codes.index(start_after_zip) + 1:]
        else:
            zip_codes = [zip_code for zip_code in zip_codes if zip_code > start_after_zip]
    zip_codes, eligibility_summary = filter_zip_codes_for_scrape(
        zip_codes,
        filter_name=args.zip_eligibility_filter,
        min_residential_or_other_ratio=args.min_residential_or_other_ratio,
        auto_build=getattr(args, "zip_eligibility_auto_build", True),
    )
    args.zip_eligibility_summary = eligibility_summary
    if eligibility_summary.get("skipped_count"):
        print(
            f"[runner] ZIP eligibility filter skipped {eligibility_summary['skipped_count']} of "
            f"{eligibility_summary['input_count']} ZIPs: {eligibility_summary.get('reason_counts')}",
            flush=True,
        )
    elif args.zip_eligibility_filter != "none" and not eligibility_summary.get("applied"):
        print(
            "[runner] ZIP eligibility filter did not run; cached HUD-USPS eligibility file is missing. "
            "Run re_analyzer.scrapers.zip_eligibility with a HUD API token to build it "
            "(env: HUD_USPS_CROSSWALK_TOKEN / HUD_USER_API_TOKEN / HUD_API_TOKEN).",
            flush=True,
        )

    selected = []
    for zip_code in zip_codes:
        if args.respect_cooldown:
            should_process = any(
                _should_process_zip_code(
                    provider,
                    zip_code,
                    args.cooldown_hours,
                    require_saved=bool(args.save),
                )
                for provider in providers
            )
            if not should_process:
                continue
        selected.append(zip_code)
        if args.max_zip_codes and args.max_zip_codes > 0 and len(selected) >= args.max_zip_codes:
            break
    return selected


def _build_command(args, provider, index, provider_count, selected_zip_codes=None):
    command = [
        sys.executable,
        "-m",
        "re_analyzer.scrapers.scraper_runner",
        "--provider",
        provider,
        "--max-pages",
        str(args.max_pages),
        "--min-delay-seconds",
        str(args.min_delay_seconds),
        "--max-delay-seconds",
        str(args.max_delay_seconds),
        "--zip-delay-seconds",
        str(args.zip_delay_seconds),
        "--sample-size",
        str(args.sample_size),
        "--empty-page-retries",
        str(args.empty_page_retries),
        "--manual-challenge-wait-seconds",
        str(args.manual_challenge_wait_seconds),
        "--session-warmup-seconds",
        str(args.session_warmup_seconds),
        "--zip-navigation-warmup-seconds",
        str(args.zip_navigation_warmup_seconds),
        "--redfin-rental-estimate-delay-seconds",
        str(args.redfin_rental_estimate_delay_seconds),
        "--diagnostics-dir",
        str(args.diagnostics_dir),
        "--driver-startup-lock",
        str(args.driver_startup_lock),
    ]
    if args.florida_refresh:
        if selected_zip_codes:
            for zip_code in selected_zip_codes:
                command.extend(["--zip-code", str(zip_code)])
        else:
            command.extend([
                "--max-zip-codes",
                str(args.max_zip_codes),
            ])
        if args.all_pages:
            command.extend([
                "--all-pages",
                "--max-discovered-pages-per-zip",
                str(args.max_discovered_pages_per_zip),
            ])
        if args.respect_cooldown and not selected_zip_codes:
            command.extend([
                "--respect-cooldown",
                "--cooldown-hours",
                str(args.cooldown_hours),
            ])
        if args.start_after_zip and not selected_zip_codes:
            command.extend(["--start-after-zip", str(args.start_after_zip)])
    else:
        command.extend(["--zip-code", str(args.zip_code)])
    if args.driver_user_multi_procs and not getattr(args, "chromedriver_path", ""):
        command.append("--driver-user-multi-procs")
    else:
        command.append("--no-driver-user-multi-procs")
    window_rect = _window_rect_for_provider(args, index, provider_count)
    if window_rect:
        command.extend([
            "--window-x",
            str(window_rect["x"]),
            "--window-y",
            str(window_rect["y"]),
            "--window-width",
            str(window_rect["width"]),
            "--window-height",
            str(window_rect["height"]),
        ])
    if args.chrome_path:
        command.extend(["--chrome-path", str(args.chrome_path)])
    if getattr(args, "chromedriver_path", ""):
        command.extend(["--chromedriver-path", str(args.chromedriver_path)])
    if args.isolate_profiles:
        profile_suffix = "florida_refresh" if args.florida_refresh else (str(args.zip_code) if args.zip_code else "session")
        use_fresh = (provider == "realtor" and getattr(args, "realtor_fresh_profile", False) and args.florida_refresh)
        profile_dir_fn = _fresh_provider_profile_dir if use_fresh else _provider_profile_dir
        profile_dir = profile_dir_fn(args.profile_root, provider, profile_suffix)
        # Seed a brand-new persistent Realtor profile with synthetic browsing history
        # so it doesn't present as a zero-history bot session on first launch.
        if provider == "realtor" and not use_fresh:
            try:
                from re_analyzer.scrapers.realtor_profile_seeder import seed_chrome_profile
                n = seed_chrome_profile(
                    str(profile_dir),
                    profile_subdir=str(args.chrome_profile_directory),
                )
                if n > 0:
                    print(f"[realtor-seeder] seeded {n} history entries into {profile_dir}", flush=True)
            except Exception as exc:
                print(f"[realtor-seeder] warning: could not seed profile ({exc})", flush=True)
        command.extend([
            "--chrome-user-data-dir",
            str(profile_dir),
            "--chrome-profile-directory",
            str(args.chrome_profile_directory),
        ])
    if args.clear_profile_cache:
        command.append("--clear-profile-cache")
    if not args.debug_snapshots:
        command.append("--no-debug-snapshots")
    if not args.stop_on_challenge:
        command.append("--no-stop-on-challenge")
    if args.stop_on_error:
        command.append("--stop-on-error")
    if args.random_profile:
        command.append("--random-profile")
    if args.clean_profile:
        command.append("--clean-profile")
    if not args.ignore_detection:
        command.append("--no-ignore-detection")
    if args.redfin_rental_estimates:
        command.append("--redfin-rental-estimates")
    else:
        command.append("--no-redfin-rental-estimates")
    command.extend([
        "--redfin-rental-estimate-limit-per-zip",
        str(args.redfin_rental_estimate_limit_per_zip),
    ])
    if provider == "zillow" and getattr(args, "zillow_property_details", False):
        command.append("--zillow-property-details")
        command.extend([
            "--zillow-property-details-limit-per-zip",
            str(getattr(args, "zillow_property_details_limit_per_zip", 25)),
            "--zillow-property-details-delay-seconds",
            str(getattr(args, "zillow_property_details_delay_seconds", 2.0)),
            "--zillow-property-details-cooldown-hours",
            str(getattr(args, "zillow_property_details_cooldown_hours", 36.0)),
        ])
        if getattr(args, "zillow_property_details_force", False):
            command.append("--zillow-property-details-force")
    if provider == "realtor":
        if getattr(args, "realtor_zip_delay_seconds", 0.0) > 0:
            command.extend(["--realtor-zip-delay-seconds", str(args.realtor_zip_delay_seconds)])
        if getattr(args, "realtor_zip_budget", 0) > 0:
            command.extend(["--realtor-zip-budget", str(args.realtor_zip_budget)])
        donor_dirs = getattr(args, "realtor_cookie_donor_profiles", None) or []
        for d in donor_dirs:
            command.extend(["--realtor-cookie-donor-profile", d])
        if getattr(args, "realtor_property_estimates", False):
            command.append("--realtor-property-estimates")
            command.extend([
                "--realtor-property-estimates-limit-per-zip",
                str(getattr(args, "realtor_property_estimates_limit_per_zip", 0)),
                "--realtor-property-estimates-delay-seconds",
                str(getattr(args, "realtor_property_estimates_delay_seconds", 0.5)),
            ])
        else:
            command.append("--no-realtor-property-estimates")
    if int(getattr(args, "max_consecutive_blocks", 0) or 0) > 0:
        command.extend(["--max-consecutive-blocks", str(args.max_consecutive_blocks)])
    if args.save:
        command.append("--save")
    if args.enforce_window_rect:
        command.append("--enforce-window-rect")
    return command


def _stream_output(provider, pipe, lines):
    for line in iter(pipe.readline, ""):
        line = line.rstrip()
        lines.append(line)
        print(f"[{provider}] {line}", flush=True)
    pipe.close()


def _parse_summary(lines):
    for index, line in enumerate(lines):
        if line.strip() == "SCRAPE_RUN_SUMMARY":
            payload = "\n".join(lines[index + 1 :]).strip()
            if not payload:
                return None
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return None
    return None


def _provider_result(provider, return_code, lines, args):
    summary = _parse_summary(lines)
    log_tail = lines[-80:]
    if summary is not None:
        return {
            "provider": provider,
            "return_code": return_code,
            "summary": summary,
            "log_tail": log_tail[-20:],
        }

    message = (
        f"{provider} process exited with code {return_code} before SCRAPE_RUN_SUMMARY."
        if return_code
        else f"{provider} process completed without SCRAPE_RUN_SUMMARY."
    )
    return {
        "provider": provider,
        "return_code": return_code if return_code is not None else 1,
        "summary": {
            "provider": provider,
            "dry_run": not args.save,
            "zip_codes_requested": len(getattr(args, "selected_zip_codes", []) or []) or (args.max_zip_codes if args.florida_refresh else 1),
            "zip_codes_processed": 0,
            "pages_requested": 0,
            "pages_processed": 0,
            "raw_listings_seen": 0,
            "canonical_listings_seen": 0,
            "saved_zip_codes": 0,
            "empty_pages": 0,
            "warnings": [],
            "errors": [message],
            "diagnostics": [],
        },
        "error_excerpt": "\n".join(log_tail[-25:]) or message,
        "log_tail": log_tail,
    }


def run_side_by_side(args):
    providers = args.providers or DEFAULT_PROVIDERS
    selected_zip_codes = _selected_zip_codes_for_aligned_refresh(args, providers)
    args.selected_zip_codes = selected_zip_codes
    processes = []
    readers = []
    results = []

    if args.florida_refresh and not selected_zip_codes:
        raise ValueError("No ZIP codes selected for this aligned Florida refresh.")

    if args.florida_refresh and selected_zip_codes:
        print(f"[runner] aligned ZIP batch: {', '.join(selected_zip_codes)}", flush=True)

    for index, provider in enumerate(providers):
        if index and args.startup_stagger_seconds > 0:
            time.sleep(args.startup_stagger_seconds)
        command = _build_command(args, provider, index, len(providers), selected_zip_codes=selected_zip_codes)
        print(f"[runner] starting {provider}: {' '.join(command)}", flush=True)
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        lines = []
        reader = threading.Thread(target=_stream_output, args=(provider, process.stdout, lines), daemon=True)
        reader.start()
        processes.append((provider, process, lines))
        readers.append(reader)

    for provider, process, lines in processes:
        return_code = process.wait()
        results.append(_provider_result(provider, return_code, lines, args))

    for reader in readers:
        reader.join(timeout=2)

    summary = {
        "zip_code": str(args.zip_code) if args.zip_code else None,
        "run_mode": "provider_florida_all" if args.florida_refresh else "side_by_side",
        "providers": results,
        "parallel_mode": "staggered" if args.startup_stagger_seconds > 0 else "simultaneous",
        "isolated_profiles": args.isolate_profiles,
        "max_zip_codes": len(selected_zip_codes) or args.max_zip_codes if args.florida_refresh else None,
        "zip_codes": selected_zip_codes if args.florida_refresh else ([str(args.zip_code)] if args.zip_code else []),
        "zip_eligibility_filter": getattr(args, "zip_eligibility_summary", {}),
    }
    if args.reconcile_after_run and args.save and selected_zip_codes:
        successful_providers = [
            result["provider"]
            for result in results
            if result.get("return_code") == 0 and result.get("provider") in RECONCILER_PROVIDERS
        ]
        if len(successful_providers) >= 2:
            try:
                reconciliation = reconcile_sources(
                    selected_zip_codes,
                    providers=tuple(successful_providers),
                    include_nearby=args.reconcile_include_nearby,
                )
                screenshot_summary = {}
                if args.reconcile_debug_screenshots and args.debug_snapshots:
                    screenshot_summary = capture_reconciliation_debug_screenshots(
                        reconciliation,
                        diagnostics_dir=args.diagnostics_dir,
                        max_cases=args.reconcile_debug_screenshot_limit,
                        warmup_seconds=args.reconcile_debug_screenshot_warmup_seconds,
                        chrome_path=args.chrome_path,
                        chrome_user_data_dir=str(Path(args.profile_root) / "reconciliation_review"),
                        window_rect={
                            "x": int(args.tile_screen_x),
                            "y": int(args.tile_screen_y),
                            "width": 900,
                            "height": int(args.tile_screen_height),
                        } if args.tile_windows else None,
                        ignore_detection=args.ignore_detection,
                    )
                reconciliation["saved_paths"] = save_reconciliation_report(reconciliation)
                summary["reconciliation"] = {
                    "status": "ok",
                    "providers": successful_providers,
                    "saved_paths": reconciliation["saved_paths"],
                    "totals": reconciliation.get("totals", {}),
                    "debug_screenshots": screenshot_summary,
                }
                print(
                    f"[runner] source reconciliation saved: {reconciliation['saved_paths'].get('json_path')}",
                    flush=True,
                )
            except Exception as exc:
                summary["reconciliation"] = {
                    "status": "error",
                    "error": str(exc),
                    "providers": successful_providers,
                }
                print(f"[runner] source reconciliation failed: {exc}", flush=True)
    return summary


def parse_args():
    parser = argparse.ArgumentParser(description="Run bounded provider scrapes side by side.")
    parser.add_argument("--zip-code")
    parser.add_argument("--florida-refresh", action="store_true")
    parser.add_argument("--providers", nargs="+", choices=DEFAULT_PROVIDERS, default=DEFAULT_PROVIDERS)
    parser.add_argument(
        "--zillow-property-details",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Zillow-only: after collecting search results, visit each listing URL and persist the property page __NEXT_DATA__ payload.",
    )
    parser.add_argument("--zillow-property-details-limit-per-zip", type=int, default=25)
    parser.add_argument("--zillow-property-details-delay-seconds", type=float, default=2.0)
    parser.add_argument("--zillow-property-details-cooldown-hours", type=float, default=36.0)
    parser.add_argument("--zillow-property-details-force", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--max-zip-codes", type=int, default=1)
    parser.add_argument("--start-after-zip", default="")
    parser.add_argument("--all-pages", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--max-discovered-pages-per-zip", type=int, default=20)
    parser.add_argument("--respect-cooldown", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--cooldown-hours", type=float, default=36.0)
    parser.add_argument("--zip-eligibility-filter", choices=["none", "hud_usps"], default="hud_usps")
    parser.add_argument(
        "--zip-eligibility-auto-build",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When HUD-USPS eligibility cache is missing, attempt to build it automatically if a HUD token is available in env vars.",
    )
    parser.add_argument("--min-residential-or-other-ratio", type=float, default=0.0)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--min-delay-seconds", type=float, default=2.0)
    parser.add_argument("--max-delay-seconds", type=float, default=4.0)
    parser.add_argument("--zip-delay-seconds", type=float, default=15.0)
    parser.add_argument("--startup-stagger-seconds", type=float, default=5.0)
    parser.add_argument("--sample-size", type=int, default=1)
    parser.add_argument("--empty-page-retries", type=int, default=0)
    parser.add_argument("--manual-challenge-wait-seconds", type=float, default=45)
    parser.add_argument("--session-warmup-seconds", type=float, default=2)
    parser.add_argument("--zip-navigation-warmup-seconds", type=float, default=1)
    parser.add_argument("--redfin-rental-estimates", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--redfin-rental-estimate-limit-per-zip", type=int, default=0)
    parser.add_argument("--redfin-rental-estimate-delay-seconds", type=float, default=0.25)
    parser.add_argument("--diagnostics-dir", default="re_analyzer/Data/ScraperDiagnostics")
    parser.add_argument("--profile-root", default="re_analyzer/Data/ScraperDiagnostics/ParallelProfiles")
    parser.add_argument("--chrome-profile-directory", default="Default")
    parser.add_argument("--chrome-path", default="")
    parser.add_argument("--chromedriver-path", default="", help="Optional pinned chromedriver binary path. Avoid UC cached driver mismatches.")
    parser.add_argument("--tile-windows", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--tile-screen-x", type=int, default=0)
    parser.add_argument("--tile-screen-y", type=int, default=0)
    parser.add_argument("--tile-screen-width", type=int, default=1728)
    parser.add_argument("--tile-screen-height", type=int, default=1050)
    parser.add_argument("--tile-gap", type=int, default=8)
    parser.add_argument("--enforce-window-rect", action="store_true", help="Slower fallback if Chrome ignores launch geometry.")
    parser.add_argument("--driver-startup-lock", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--driver-user-multi-procs", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--isolate-profiles", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--debug-snapshots", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--stop-on-challenge", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--random-profile", action="store_true")
    parser.add_argument("--clean-profile", action="store_true")
    parser.add_argument("--clear-profile-cache", action="store_true")
    parser.add_argument("--ignore-detection", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--reconcile-after-run", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--reconcile-include-nearby", action="store_true")
    parser.add_argument("--reconcile-debug-screenshots", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--reconcile-debug-screenshot-limit", type=int, default=12)
    parser.add_argument("--reconcile-debug-screenshot-warmup-seconds", type=float, default=2.0)
    parser.add_argument("--realtor-zip-delay-seconds", type=float, default=0.0, help="Override --zip-delay-seconds for realtor only. 0 = use --zip-delay-seconds.")
    parser.add_argument("--realtor-zip-budget", type=int, default=0, help="Stop the realtor session after this many fetched ZIPs. 0 = no limit.")
    parser.add_argument("--realtor-fresh-profile", action=argparse.BooleanOptionalAction, default=False, help="Use a timestamped Chrome user-data-dir per realtor run. Disabled by default: a persistent profile accumulates cookies and history that reduce bot-detection scores.")
    parser.add_argument("--realtor-cookie-donor-profiles", dest="realtor_cookie_donor_profiles", metavar="DIR", action="append", default=[], help="Chrome user-data-dir(s) to donate safe analytics cookies from. Can be specified multiple times. Each DIR should contain a 'Default' (or --chrome-profile-directory) subdirectory with a Cookies DB. Cookies are filtered to remove any PerimeterX/KPSDK/auth tokens before injection.")
    parser.add_argument("--realtor-property-estimates", action=argparse.BooleanOptionalAction, default=False, help="Fetch Realtor DPPropertyEstimates GraphQL for each listing (Quantarium/Cotality/Collateral Analytics current+historical+forecast AVM values).")
    parser.add_argument("--realtor-property-estimates-limit-per-zip", type=int, default=0, help="Safety cap for Realtor property estimate enrichment per ZIP. Use 0 for all listings.")
    parser.add_argument("--realtor-property-estimates-delay-seconds", type=float, default=0.5, help="Pacing delay between Realtor DPPropertyEstimates requests.")
    parser.add_argument("--max-consecutive-blocks", type=int, default=0, help="Abort a provider's run after this many consecutive block responses. 0 = no limit. Default 3 for Realtor when launched from the control server.")
    return parser.parse_args()


def main():
    summary = run_side_by_side(parse_args())
    print("SIDE_BY_SIDE_RUN_SUMMARY")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
