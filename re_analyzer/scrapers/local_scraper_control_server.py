import argparse
import hmac
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request
from re_analyzer.scrapers.scraping_utility import (
    _bundled_chrome_for_testing_binary,
    _bundled_chromedriver_binary,
    _existing_env_path,
)


RUNS = {}
RUN_LOCK = threading.Lock()
PROBE_RUNS = {}
PROBE_RUN_LOCK = threading.Lock()
LOG_LIMIT = int(os.environ.get("LOCAL_SCRAPER_LOG_LIMIT", "2000"))


def _detect_binary_version(path: str) -> str:
    binary = str(path or "").strip()
    if not binary:
        return ""
    expanded = os.path.expanduser(binary)
    if not os.path.exists(expanded):
        resolved = shutil.which(binary)
        if not resolved:
            return ""
        expanded = resolved
    try:
        proc = subprocess.run(
            [expanded, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except Exception:
        return ""
    output = (proc.stdout or proc.stderr or "").strip()
    if not output:
        return ""
    return output.splitlines()[0].strip()


def _parse_major_version(version_text: str) -> Optional[int]:
    text = str(version_text or "").strip()
    if not text:
        return None
    match = re.search(r"\b(\d{2,4})\.", text)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


_BROWSER_BINARIES_CACHE = {
    "timestamp": 0.0,
    "ttl_seconds": 30.0,
    "data": None,
}


def _collect_browser_binaries():
    chrome_candidates = []
    driver_candidates = []

    def add_candidate(bucket, *, path: str, label: str, kind: str):
        value = str(path or "").strip()
        if not value:
            return
        expanded = os.path.expanduser(value)
        if not os.path.exists(expanded):
            resolved = shutil.which(value)
            if not resolved:
                return
            expanded = resolved
        if any(item.get("path") == expanded for item in bucket):
            return
        version_text = _detect_binary_version(expanded)
        bucket.append({
            "path": expanded,
            "label": label,
            "kind": kind,
            "version": version_text or None,
            "major": _parse_major_version(version_text) if version_text else None,
        })

    default_chrome = str(DEFAULT_CHROME_PATH or "").strip()
    if default_chrome:
        add_candidate(chrome_candidates, path=default_chrome, label="Default chrome", kind="default")

    bundled_chrome = _bundled_chrome_for_testing_binary()
    if bundled_chrome:
        add_candidate(chrome_candidates, path=bundled_chrome, label="Bundled chrome-for-testing", kind="bundled")

    system = platform.system()
    if system == "Darwin":
        for candidate, label in (
            ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome"),
            ("/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing", "Chrome for Testing (system)"),
            ("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary", "Google Chrome Canary"),
            ("/Applications/Chromium.app/Contents/MacOS/Chromium", "Chromium"),
        ):
            add_candidate(chrome_candidates, path=candidate, label=label, kind="system")
    elif system == "Windows":
        prefixes = []
        for env_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            value = os.environ.get(env_key)
            if value:
                prefixes.append(value)
        for suffix, label in (
            (os.path.join("Google", "Chrome", "Application", "chrome.exe"), "Google Chrome"),
            (os.path.join("Chromium", "Application", "chrome.exe"), "Chromium"),
        ):
            for prefix in prefixes:
                add_candidate(chrome_candidates, path=os.path.join(prefix, suffix), label=label, kind="system")
    else:
        for candidate, label in (
            ("google-chrome", "google-chrome"),
            ("google-chrome-stable", "google-chrome-stable"),
            ("chromium", "chromium"),
            ("chromium-browser", "chromium-browser"),
        ):
            add_candidate(chrome_candidates, path=candidate, label=label, kind="system")

    driver_env = _existing_env_path("CHROMEDRIVER_EXECUTABLE_PATH", "RE_ANALYZER_CHROMEDRIVER_PATH")
    if driver_env:
        add_candidate(driver_candidates, path=driver_env, label="Configured chromedriver", kind="env")

    bundled_driver = _bundled_chromedriver_binary()
    if bundled_driver:
        add_candidate(driver_candidates, path=bundled_driver, label="Bundled chromedriver", kind="bundled")

    add_candidate(driver_candidates, path="chromedriver", label="chromedriver (PATH)", kind="system")

    # Prefer a chromedriver whose major version matches the default chrome.
    # If we can't find a compatible driver, leave the default empty so upstream
    # can fall back to Selenium Manager / undetected-chromedriver managed driver
    # instead of failing with a version mismatch.
    default_chrome_major = None
    try:
        default_chrome_major = next(
            (item.get("major") for item in chrome_candidates if item.get("path") == os.path.expanduser(default_chrome)),
            None,
        )
    except Exception:
        default_chrome_major = None

    def pick_matching_driver(preferred_path: str | None) -> str:
        if not preferred_path:
            return ""
        expanded = os.path.expanduser(preferred_path)
        for item in driver_candidates:
            if item.get("path") == expanded:
                return expanded
        resolved = shutil.which(preferred_path)
        return resolved or ""

    def find_driver_for_major(major: int | None) -> str:
        if not major:
            return ""
        for item in driver_candidates:
            if item.get("major") == major and item.get("path"):
                return str(item["path"])
        return ""

    default_driver = pick_matching_driver(driver_env) or pick_matching_driver(bundled_driver) or (shutil.which("chromedriver") or "")
    if default_chrome_major:
        matching = find_driver_for_major(default_chrome_major)
        if matching:
            default_driver = matching
        else:
            # If we have a driver but it's incompatible, do not auto-select it.
            try:
                selected = next((item for item in driver_candidates if item.get("path") == default_driver), None)
                if selected and selected.get("major") and selected.get("major") != default_chrome_major:
                    default_driver = ""
            except Exception:
                pass

    return {
        "chrome_path": default_chrome,
        "chromedriver_path": default_driver,
        "defaults": {
            "chrome_major": default_chrome_major,
            "chromedriver_major": _parse_major_version(_detect_binary_version(default_driver)) if default_driver else None,
        },
        "chrome": chrome_candidates,
        "chromedriver": driver_candidates,
    }


def browser_binaries_snapshot():
    now = time.time()
    cached = _BROWSER_BINARIES_CACHE.get("data")
    if cached and now - float(_BROWSER_BINARIES_CACHE.get("timestamp") or 0) < float(_BROWSER_BINARIES_CACHE.get("ttl_seconds") or 30):
        return cached
    data = _collect_browser_binaries()
    _BROWSER_BINARIES_CACHE["timestamp"] = now
    _BROWSER_BINARIES_CACHE["data"] = data
    return data


DEFAULT_CHROME_PATH = (
    os.environ.get("LOCAL_SCRAPER_CHROME_PATH")
    or _bundled_chrome_for_testing_binary()
    or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)
ALLOWED_ORIGINS = {
    "http://localhost",
    "http://localhost:80",
    "http://localhost:3000",
    "http://localhost:8080",
    "http://127.0.0.1",
    "http://127.0.0.1:80",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8080",
}
PROVIDERS = {"zillow", "redfin", "realtor"}
PROFILE_ROOT = Path("re_analyzer/Data/ScraperDiagnostics/ParallelProfiles")
RESUME_PROGRESS_PATH = Path("re_analyzer/Data/ScraperDiagnostics/resume_progress.json")
RESUME_PROGRESS_LOCK = threading.Lock()
PROBE_ROOT = Path("re_analyzer/Data/DetectionProbe/ProbeRuns")
CONTROL_TOKEN = os.environ.get("LOCAL_SCRAPER_CONTROL_TOKEN", "").strip()
REQUIRE_TOKEN_FOR_MUTATIONS = os.environ.get("LOCAL_SCRAPER_REQUIRE_TOKEN_FOR_MUTATIONS", "true").strip().lower() not in {"0", "false", "no", "off"}

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if is_allowed_local_origin(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


@app.before_request
def require_control_token():
    if request.method == "OPTIONS":
        return None
    token_required = bool(CONTROL_TOKEN) or (REQUIRE_TOKEN_FOR_MUTATIONS and request.method in {"POST", "DELETE", "PUT", "PATCH"})
    if not token_required:
        return None
    if not CONTROL_TOKEN:
        return jsonify({"error": "Set LOCAL_SCRAPER_CONTROL_TOKEN before using mutating scraper controller routes."}), 503

    auth_header = request.headers.get("Authorization", "")
    submitted = ""
    if auth_header.lower().startswith("bearer "):
        submitted = auth_header[7:].strip()
    submitted = submitted or request.headers.get("X-Local-Scraper-Token", "").strip()
    if not hmac.compare_digest(submitted, CONTROL_TOKEN):
        return jsonify({"error": "Local scraper control token required."}), 401
    return None


@app.route("/api/<path:_path>", methods=["OPTIONS"])
def options(_path):
    return ("", 204)


def is_allowed_local_origin(origin):
    if not origin:
        return False
    if origin in ALLOWED_ORIGINS:
        return True
    return bool(re.fullmatch(r"http://(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?", origin))


def is_loopback_bind_host(host):
    host = str(host or "").strip().lower()
    return host in {"", "localhost", "127.0.0.1", "::1"}


def network_bind_requires_token(host):
    return not is_loopback_bind_host(host)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_summary(lines):
    markers = {"SIDE_BY_SIDE_RUN_SUMMARY", "SCRAPE_RUN_SUMMARY", "PROBE_RUN_SUMMARY"}
    for index, line in enumerate(lines):
        if line.strip() in markers:
            payload = "\n".join(lines[index + 1:]).strip()
            if not payload:
                return None
            try:
                summary = json.loads(payload)
            except json.JSONDecodeError:
                return None
            if line.strip() == "PROBE_RUN_SUMMARY":
                return {
                    "probe_summary": summary,
                    "kind": "probe",
                }
            if line.strip() == "SCRAPE_RUN_SUMMARY":
                return {
                    "providers": [{
                        "provider": summary.get("provider", "zillow"),
                        "return_code": 0 if not summary.get("errors") else 1,
                        "summary": summary,
                    }],
                    "single_provider": True,
                    "scrape_summary": summary,
                }
            return summary
    return None


def public_run(run):
    return {
        "kind": run.get("kind", "scrape"),
        "run_id": run["run_id"],
        "status": run["status"],
        "created_at": run["created_at"],
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "duration_seconds": run.get("duration_seconds"),
        "return_code": run.get("return_code"),
        "command_preview": run.get("command_preview"),
        "config": run.get("config", {}),
        "last_zip_code": run.get("last_zip_code"),
        "summary": run.get("summary"),
        "error": run.get("error"),
        "logs": list(run.get("logs", [])),
    }


def active_count():
    with RUN_LOCK:
        return sum(1 for run in RUNS.values() if run.get("status") in {"starting", "running"})


def active_probe_count():
    with PROBE_RUN_LOCK:
        return sum(1 for run in PROBE_RUNS.values() if run.get("status") in {"starting", "running"})


def active_total_count():
    return active_count() + active_probe_count()


def latest_reconciliation_report():
    report_dir = Path(__file__).resolve().parents[1] / "Data" / "Fetched" / "Reconciliation"
    paths = sorted(report_dir.glob("source_reconciliation_*.json"), key=lambda item: item.stat().st_mtime)
    if not paths:
        return None, {"error": "No source reconciliation reports found.", "report_dir": str(report_dir)}

    path = paths[-1]
    try:
        with open(path, "r", encoding="utf-8") as file:
            report = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        return None, {"error": f"Unable to read source reconciliation report: {exc}", "path": str(path)}

    return {
        "status": "ok",
        "path": str(path),
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        "report": report,
    }, None


def append_log(run, line):
    run["logs"].append(line)
    matches = re.findall(r"\bZIP\s+(\d{5})\b", line or "")
    if matches:
        latest_zip = matches[-1]
        run["last_zip_code"] = latest_zip
        # Only advance the persistent resume pointer when the runner emits an explicit
        # completion checkpoint. Updating on any "ZIP XXXXX" mention would record
        # in-progress ZIPs as the resume point, causing them to be skipped on the
        # next run even if they were only partially processed before the abort.
        if "[resume-checkpoint]" in (line or ""):
            _record_resume_progress(run, latest_zip)
    if len(run["logs"]) > LOG_LIMIT:
        del run["logs"][: len(run["logs"]) - LOG_LIMIT]


def append_probe_log(run, line):
    run["logs"].append(line)
    if len(run["logs"]) > LOG_LIMIT:
        del run["logs"][: len(run["logs"]) - LOG_LIMIT]


def _load_resume_progress():
    try:
        if not RESUME_PROGRESS_PATH.exists():
            return {}
        with open(RESUME_PROGRESS_PATH, "r", encoding="utf-8") as file:
            return json.load(file) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_resume_progress(data: dict):
    RESUME_PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = RESUME_PROGRESS_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True)
    tmp_path.replace(RESUME_PROGRESS_PATH)


def _record_resume_progress(run: dict, zip_code: str):
    config = run.get("config") or {}
    run_mode = str(config.get("run_mode") or "")
    refresh_provider = str(config.get("refresh_provider") or "")
    if not run_mode.startswith("provider_florida") or not refresh_provider:
        return
    zip_code = str(zip_code or "").strip()
    if not re.fullmatch(r"\d{5}", zip_code):
        return

    with RESUME_PROGRESS_LOCK:
        data = _load_resume_progress()
        by_provider = data.setdefault("by_refresh_provider", {})
        by_provider[refresh_provider] = {
            "zip_code": zip_code,
            "updated_at": now_iso(),
            "run_id": run.get("run_id", ""),
            "run_mode": run_mode,
        }
        data["updated_at"] = now_iso()
        _save_resume_progress(data)


def _resolve_auto_resume_zip(payload: dict, *, refresh_provider: str) -> str:
    if str(payload.get("start_after_zip") or "").strip():
        return str(payload.get("start_after_zip") or "").strip()
    if not bool(payload.get("auto_resume_after_latest_zip", False)):
        return ""
    with RESUME_PROGRESS_LOCK:
        data = _load_resume_progress()
    entry = (data.get("by_refresh_provider") or {}).get(str(refresh_provider or "")) or {}
    zip_code = str(entry.get("zip_code") or "").strip()
    if re.fullmatch(r"\d{5}", zip_code):
        return zip_code

    def latest_zip_from_metadata(provider: str) -> str:
        meta_dir = Path(__file__).resolve().parents[1] / "Data" / "Fetched" / provider / "Metadata"
        if not meta_dir.exists():
            return ""
        candidates = list(meta_dir.glob("*_metadata.json"))
        if not candidates:
            return ""
        latest = max(candidates, key=lambda path: path.stat().st_mtime)
        inferred = latest.name.replace("_metadata.json", "")
        return inferred if re.fullmatch(r"\d{5}", inferred) else ""

    if str(refresh_provider or "") == "all":
        latest_per_provider = [
            latest_zip_from_metadata(provider)
            for provider in sorted(PROVIDERS)
        ]
        latest_per_provider = [zip_code for zip_code in latest_per_provider if re.fullmatch(r"\d{5}", zip_code)]
        if not latest_per_provider:
            return ""
        return sorted(latest_per_provider, key=lambda value: int(value))[0]

    fallback = latest_zip_from_metadata(str(refresh_provider))
    return fallback if re.fullmatch(r"\d{5}", fallback) else ""


def bounded_float(value, default, minimum, maximum):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = default
    return min(max(numeric, minimum), maximum)


def bounded_int(value, default, minimum, maximum):
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = default
    return min(max(numeric, minimum), maximum)


def provider_profile_dir(provider, suffix):
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(suffix or "session")).strip("_") or "session"
    return PROFILE_ROOT / f"{provider}_{safe_suffix}"


def fresh_provider_profile_dir(provider, suffix):
    """Return a timestamped profile directory that is unique per run."""
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(suffix or "session")).strip("_") or "session"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return PROFILE_ROOT / f"{provider}_{safe_suffix}_{ts}"


def _normalize_probe_urls(payload):
    raw = payload.get("urls") if isinstance(payload, dict) else None
    if raw is None:
        raw = payload.get("url") if isinstance(payload, dict) else None
    urls = []
    if isinstance(raw, str):
        chunks = re.split(r"[,\s]+", raw.strip())
        urls = [chunk.strip() for chunk in chunks if chunk.strip()]
    elif isinstance(raw, list):
        urls = [str(item or "").strip() for item in raw if str(item or "").strip()]
    else:
        urls = []

    seen = set()
    normalized = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        normalized.append(url)
    return normalized


def _resolve_chromedriver_path(*, chrome_path: str, chromedriver_path: str) -> str:
    requested = str(chromedriver_path or "").strip()
    if requested:
        return requested

    try:
        binaries = browser_binaries_snapshot() or {}
    except Exception:
        binaries = {}

    chrome_major = None
    try:
        chrome_path_clean = str(chrome_path or "").strip()
        for item in (binaries.get("chrome") or []):
            if str(item.get("path") or "").strip() == chrome_path_clean:
                chrome_major = item.get("major")
                break
    except Exception:
        chrome_major = None

    if chrome_major:
        try:
            for item in (binaries.get("chromedriver") or []):
                if item.get("major") == chrome_major and item.get("path"):
                    return str(item["path"]).strip()
        except Exception:
            pass
        # We know the Chrome major but couldn't find a matching driver in our snapshot;
        # returning an incompatible fallback is worse than returning nothing.
        return ""

    fallback = str(binaries.get("chromedriver_path") or "").strip()
    return fallback


def build_probe_command(payload, run_id: str):
    urls = _normalize_probe_urls(payload)
    if not urls:
        raise ValueError("Provide at least one URL to probe.")

    headless = bool(payload.get("headless", True))
    chrome_path = str(payload.get("chrome_path") or DEFAULT_CHROME_PATH).strip()
    chromedriver_path = _resolve_chromedriver_path(
        chrome_path=chrome_path,
        chromedriver_path=str(payload.get("chromedriver_path") or "").strip(),
    )

    output_dir = str(payload.get("output_dir") or "").strip()
    if output_dir:
        output_path = Path(output_dir).expanduser()
    else:
        output_path = PROBE_ROOT / run_id

    command = [
        sys.executable,
        "-m",
        "re_analyzer.scrapers.detection_probe",
        "--output-dir",
        str(output_path),
        "--urls",
        *urls,
    ]
    interaction_test = bool(payload.get("interaction_test", False))

    if not headless:
        command.append("--no-headless")
    if chrome_path:
        command.extend(["--chrome-path", chrome_path])
    if chromedriver_path:
        command.extend(["--chromedriver-path", chromedriver_path])
    if interaction_test:
        command.append("--interaction-test")

    return command, {
        "run_mode": "probe",
        "headless": headless,
        "urls": urls,
        "output_dir": str(output_path),
        "chrome_path": chrome_path,
        "chromedriver_path": chromedriver_path,
        "interaction_test": interaction_test,
    }


def build_command(payload):
    run_mode = str(payload.get("run_mode") or "side_by_side").strip()
    if run_mode in {"zillow_florida", "provider_florida"}:
        return build_provider_florida_command(payload)

    zip_code = str(payload.get("zip_code") or "32404").strip()
    if not re.fullmatch(r"\d{5}", zip_code):
        raise ValueError("ZIP code must be exactly five digits.")

    providers = payload.get("providers") or ["zillow", "redfin", "realtor"]
    providers = [provider for provider in providers if provider in PROVIDERS]
    if not providers:
        raise ValueError("Select at least one supported provider.")

    max_pages = bounded_int(payload.get("max_pages"), 1, 1, 3)
    sample_size = bounded_int(payload.get("sample_size"), 0, 0, 5)
    startup_stagger_seconds = bounded_float(payload.get("startup_stagger_seconds"), 0.5, 0, 10)
    min_delay_seconds = bounded_float(payload.get("min_delay_seconds"), 1.0, 0.2, 20)
    max_delay_seconds = bounded_float(payload.get("max_delay_seconds"), max(2.0, min_delay_seconds), min_delay_seconds, 30)
    manual_wait_seconds = bounded_float(payload.get("manual_challenge_wait_seconds"), 45, 0, 180)
    session_warmup_seconds = bounded_float(payload.get("session_warmup_seconds"), 2, 0, 60)
    zip_navigation_warmup_seconds = bounded_float(payload.get("zip_navigation_warmup_seconds"), 1, 0, 60)
    redfin_rental_estimates = bool(payload.get("redfin_rental_estimates", True))
    redfin_rental_estimate_limit_per_zip = bounded_int(payload.get("redfin_rental_estimate_limit_per_zip"), 0, 0, 10000)
    redfin_rental_estimate_delay_seconds = bounded_float(payload.get("redfin_rental_estimate_delay_seconds"), 0.25, 0, 10)
    clear_profile_cache = bool(payload.get("clear_profile_cache"))
    debug_snapshots = bool(payload.get("debug_snapshots", True))
    zillow_property_details = bool(payload.get("zillow_property_details", False))
    zillow_property_details_limit_per_zip = bounded_int(payload.get("zillow_property_details_limit_per_zip"), 25, 0, 500)
    zillow_property_details_delay_seconds = bounded_float(payload.get("zillow_property_details_delay_seconds"), 2.0, 0, 20)
    zillow_property_details_cooldown_hours = bounded_float(payload.get("zillow_property_details_cooldown_hours"), 36.0, 0, 168)
    zillow_property_details_force = bool(payload.get("zillow_property_details_force", False))
    chrome_path = str(payload.get("chrome_path") or DEFAULT_CHROME_PATH).strip()
    chromedriver_path = _resolve_chromedriver_path(
        chrome_path=chrome_path,
        chromedriver_path=str(payload.get("chromedriver_path") or "").strip(),
    )

    command = [
        sys.executable,
        "-m",
        "re_analyzer.scrapers.side_by_side_scraper_runner",
        "--zip-code",
        zip_code,
        "--providers",
        *providers,
    ]
    if zillow_property_details:
        command.extend([
            "--zillow-property-details",
            "--zillow-property-details-limit-per-zip",
            str(zillow_property_details_limit_per_zip),
            "--zillow-property-details-delay-seconds",
            str(zillow_property_details_delay_seconds),
            "--zillow-property-details-cooldown-hours",
            str(zillow_property_details_cooldown_hours),
        ])
        if zillow_property_details_force:
            command.append("--zillow-property-details-force")
    command.extend([
        "--max-pages",
        str(max_pages),
        "--sample-size",
        str(sample_size),
        "--startup-stagger-seconds",
        str(startup_stagger_seconds),
        "--min-delay-seconds",
        str(min_delay_seconds),
        "--max-delay-seconds",
        str(max_delay_seconds),
        "--manual-challenge-wait-seconds",
        str(manual_wait_seconds),
        "--session-warmup-seconds",
        str(session_warmup_seconds),
        "--zip-navigation-warmup-seconds",
        str(zip_navigation_warmup_seconds),
        "--redfin-rental-estimate-limit-per-zip",
        str(redfin_rental_estimate_limit_per_zip),
        "--redfin-rental-estimate-delay-seconds",
        str(redfin_rental_estimate_delay_seconds),
        "--chrome-profile-directory",
        "Default",
        "--chrome-path",
        chrome_path,
    ])
    if chromedriver_path:
        command.extend(["--chromedriver-path", chromedriver_path])
    command.append("--redfin-rental-estimates" if redfin_rental_estimates else "--no-redfin-rental-estimates")
    if not debug_snapshots:
        command.append("--no-debug-snapshots")
    if payload.get("enforce_window_rect"):
        command.append("--enforce-window-rect")
    if payload.get("save_results"):
        command.append("--save")
    if clear_profile_cache:
        command.append("--clear-profile-cache")

    return command, {
        "run_mode": "side_by_side",
        "zip_code": zip_code,
        "providers": providers,
        "zillow_property_details": zillow_property_details,
        "zillow_property_details_limit_per_zip": zillow_property_details_limit_per_zip,
        "zillow_property_details_delay_seconds": zillow_property_details_delay_seconds,
        "zillow_property_details_cooldown_hours": zillow_property_details_cooldown_hours,
        "zillow_property_details_force": zillow_property_details_force,
        "max_pages": max_pages,
        "sample_size": sample_size,
        "startup_stagger_seconds": startup_stagger_seconds,
        "min_delay_seconds": min_delay_seconds,
        "max_delay_seconds": max_delay_seconds,
        "manual_challenge_wait_seconds": manual_wait_seconds,
        "session_warmup_seconds": session_warmup_seconds,
        "zip_navigation_warmup_seconds": zip_navigation_warmup_seconds,
        "redfin_rental_estimates": redfin_rental_estimates,
        "redfin_rental_estimate_limit_per_zip": redfin_rental_estimate_limit_per_zip,
        "redfin_rental_estimate_delay_seconds": redfin_rental_estimate_delay_seconds,
        "clear_profile_cache": clear_profile_cache,
        "debug_snapshots": debug_snapshots,
        "save_results": bool(payload.get("save_results")),
        "chrome_path": chrome_path,
        "chromedriver_path": chromedriver_path,
    }


def build_provider_florida_command(payload):
    provider = str(payload.get("refresh_provider") or payload.get("provider") or "zillow").strip().lower()
    if provider == "all":
        return build_all_provider_florida_command(payload)
    if provider not in PROVIDERS:
        raise ValueError("Select one supported provider for the Florida refresh.")
    max_zip_codes = bounded_int(payload.get("max_zip_codes"), 25, 0, 5000)
    max_pages = bounded_int(payload.get("max_pages"), 1, 1, 10)
    max_discovered_pages = bounded_int(payload.get("max_discovered_pages_per_zip"), 20, 1, 20)
    sample_size = bounded_int(payload.get("sample_size"), 0, 0, 5)
    min_delay_seconds = bounded_float(payload.get("min_delay_seconds"), 3.0, 0.5, 30)
    max_delay_seconds = bounded_float(payload.get("max_delay_seconds"), max(6.0, min_delay_seconds), min_delay_seconds, 60)
    zip_delay_seconds = bounded_float(payload.get("zip_delay_seconds"), 20.0, 0, 180)
    manual_wait_seconds = bounded_float(payload.get("manual_challenge_wait_seconds"), 45, 0, 300)
    session_warmup_seconds = bounded_float(payload.get("session_warmup_seconds"), 2, 0, 60)
    zip_navigation_warmup_seconds = bounded_float(payload.get("zip_navigation_warmup_seconds"), 1, 0, 60)
    redfin_rental_estimates = bool(payload.get("redfin_rental_estimates", True))
    redfin_rental_estimate_limit_per_zip = bounded_int(payload.get("redfin_rental_estimate_limit_per_zip"), 0, 0, 10000)
    redfin_rental_estimate_delay_seconds = bounded_float(payload.get("redfin_rental_estimate_delay_seconds"), 0.25, 0, 10)
    clear_profile_cache = bool(payload.get("clear_profile_cache"))
    zip_eligibility_filter = "hud_usps" if payload.get("zip_eligibility_filter", "hud_usps") != "none" else "none"
    cooldown_hours = bounded_float(payload.get("cooldown_hours"), 36, 0, 168)
    debug_snapshots = bool(payload.get("debug_snapshots", True))
    zillow_property_details = bool(payload.get("zillow_property_details", False))
    zillow_property_details_limit_per_zip = bounded_int(payload.get("zillow_property_details_limit_per_zip"), 25, 0, 500)
    zillow_property_details_delay_seconds = bounded_float(payload.get("zillow_property_details_delay_seconds"), 2.0, 0, 20)
    zillow_property_details_cooldown_hours = bounded_float(payload.get("zillow_property_details_cooldown_hours"), 36.0, 0, 168)
    zillow_property_details_force = bool(payload.get("zillow_property_details_force", False))
    realtor_zip_delay_seconds = bounded_float(payload.get("realtor_zip_delay_seconds"), 0.0, 0, 300) if provider == "realtor" else 0.0
    realtor_zip_budget = bounded_int(payload.get("realtor_zip_budget"), 0, 0, 5000) if provider == "realtor" else 0
    realtor_fresh_profile = bool(payload.get("realtor_fresh_profile", True)) if provider == "realtor" else False
    _default_max_consecutive = 3 if provider == "realtor" else 0
    max_consecutive_blocks = bounded_int(payload.get("max_consecutive_blocks"), _default_max_consecutive, 0, 20)
    chrome_path = str(payload.get("chrome_path") or DEFAULT_CHROME_PATH).strip()
    chromedriver_path = _resolve_chromedriver_path(
        chrome_path=chrome_path,
        chromedriver_path=str(payload.get("chromedriver_path") or "").strip(),
    )
    start_after_zip = _resolve_auto_resume_zip(payload, refresh_provider=provider)

    command = [
        sys.executable,
        "-m",
        "re_analyzer.scrapers.scraper_runner",
        "--provider",
        provider,
    ]
    if provider == "zillow" and zillow_property_details:
        command.extend([
            "--zillow-property-details",
            "--zillow-property-details-limit-per-zip",
            str(zillow_property_details_limit_per_zip),
            "--zillow-property-details-delay-seconds",
            str(zillow_property_details_delay_seconds),
            "--zillow-property-details-cooldown-hours",
            str(zillow_property_details_cooldown_hours),
        ])
        if zillow_property_details_force:
            command.append("--zillow-property-details-force")
    command.extend([
        "--max-zip-codes",
        str(max_zip_codes),
        "--max-pages",
        str(max_pages),
        "--sample-size",
        str(sample_size),
        "--min-delay-seconds",
        str(min_delay_seconds),
        "--max-delay-seconds",
        str(max_delay_seconds),
        "--zip-delay-seconds",
        str(zip_delay_seconds),
        "--manual-challenge-wait-seconds",
        str(manual_wait_seconds),
        "--session-warmup-seconds",
        str(session_warmup_seconds),
        "--zip-navigation-warmup-seconds",
        str(zip_navigation_warmup_seconds),
        "--redfin-rental-estimate-limit-per-zip",
        str(redfin_rental_estimate_limit_per_zip),
        "--redfin-rental-estimate-delay-seconds",
        str(redfin_rental_estimate_delay_seconds),
        "--zip-eligibility-filter",
        zip_eligibility_filter,
        "--chrome-path",
        chrome_path,
        "--chrome-user-data-dir",
        str(fresh_provider_profile_dir(provider, "florida_refresh") if realtor_fresh_profile else provider_profile_dir(provider, "florida_refresh")),
        "--chrome-profile-directory",
        "Default",
        "--no-stop-on-challenge",
    ])
    if chromedriver_path:
        command.extend(["--chromedriver-path", chromedriver_path])
    command.append("--redfin-rental-estimates" if redfin_rental_estimates else "--no-redfin-rental-estimates")
    if payload.get("all_pages", True):
        command.extend([
            "--all-pages",
            "--max-discovered-pages-per-zip",
            str(max_discovered_pages),
        ])
    if payload.get("respect_cooldown", True):
        command.extend([
            "--respect-cooldown",
            "--cooldown-hours",
            str(cooldown_hours),
        ])
    if start_after_zip:
        if not re.fullmatch(r"\d{5}", start_after_zip):
            raise ValueError("Resume ZIP must be exactly five digits.")
        command.extend(["--start-after-zip", start_after_zip])
    if realtor_zip_delay_seconds > 0:
        command.extend(["--realtor-zip-delay-seconds", str(realtor_zip_delay_seconds)])
    if realtor_zip_budget > 0:
        command.extend(["--realtor-zip-budget", str(realtor_zip_budget)])
    if max_consecutive_blocks > 0:
        command.extend(["--max-consecutive-blocks", str(max_consecutive_blocks)])
    if not debug_snapshots:
        command.append("--no-debug-snapshots")
    if payload.get("save_results"):
        command.append("--save")
    if clear_profile_cache:
        command.append("--clear-profile-cache")

    return command, {
        "run_mode": "provider_florida",
        "refresh_provider": provider,
        "providers": [provider],
        "zillow_property_details": zillow_property_details if provider == "zillow" else False,
        "zillow_property_details_limit_per_zip": zillow_property_details_limit_per_zip if provider == "zillow" else 0,
        "zillow_property_details_delay_seconds": zillow_property_details_delay_seconds if provider == "zillow" else 0,
        "zillow_property_details_cooldown_hours": zillow_property_details_cooldown_hours if provider == "zillow" else 0,
        "zillow_property_details_force": zillow_property_details_force if provider == "zillow" else False,
        "max_zip_codes": max_zip_codes,
        "all_pages": bool(payload.get("all_pages", True)),
        "max_pages": max_pages,
        "max_discovered_pages_per_zip": max_discovered_pages,
        "sample_size": sample_size,
        "min_delay_seconds": min_delay_seconds,
        "max_delay_seconds": max_delay_seconds,
        "zip_delay_seconds": zip_delay_seconds,
        "manual_challenge_wait_seconds": manual_wait_seconds,
        "session_warmup_seconds": session_warmup_seconds,
        "zip_navigation_warmup_seconds": zip_navigation_warmup_seconds,
        "redfin_rental_estimates": redfin_rental_estimates,
        "redfin_rental_estimate_limit_per_zip": redfin_rental_estimate_limit_per_zip,
        "redfin_rental_estimate_delay_seconds": redfin_rental_estimate_delay_seconds,
        "clear_profile_cache": clear_profile_cache,
        "zip_eligibility_filter": zip_eligibility_filter,
        "stop_on_challenge": False,
        "respect_cooldown": bool(payload.get("respect_cooldown", True)),
        "cooldown_hours": cooldown_hours,
        "start_after_zip": start_after_zip,
        "debug_snapshots": debug_snapshots,
        "reconcile_debug_screenshots": bool(payload.get("reconcile_debug_screenshots", True)),
        "save_results": bool(payload.get("save_results")),
        "chrome_path": chrome_path,
        "chromedriver_path": chromedriver_path,
        "realtor_zip_delay_seconds": realtor_zip_delay_seconds,
        "realtor_zip_budget": realtor_zip_budget,
        "realtor_fresh_profile": realtor_fresh_profile,
        "max_consecutive_blocks": max_consecutive_blocks,
    }


def build_all_provider_florida_command(payload):
    max_zip_codes = bounded_int(payload.get("max_zip_codes"), 25, 0, 5000)
    max_pages = bounded_int(payload.get("max_pages"), 1, 1, 10)
    max_discovered_pages = bounded_int(payload.get("max_discovered_pages_per_zip"), 20, 1, 20)
    sample_size = bounded_int(payload.get("sample_size"), 0, 0, 5)
    startup_stagger_seconds = bounded_float(payload.get("startup_stagger_seconds"), 0.5, 0, 30)
    min_delay_seconds = bounded_float(payload.get("min_delay_seconds"), 3.0, 0.5, 30)
    max_delay_seconds = bounded_float(payload.get("max_delay_seconds"), max(6.0, min_delay_seconds), min_delay_seconds, 60)
    zip_delay_seconds = bounded_float(payload.get("zip_delay_seconds"), 20.0, 0, 180)
    manual_wait_seconds = bounded_float(payload.get("manual_challenge_wait_seconds"), 45, 0, 300)
    session_warmup_seconds = bounded_float(payload.get("session_warmup_seconds"), 2, 0, 60)
    zip_navigation_warmup_seconds = bounded_float(payload.get("zip_navigation_warmup_seconds"), 1, 0, 60)
    redfin_rental_estimates = bool(payload.get("redfin_rental_estimates", True))
    redfin_rental_estimate_limit_per_zip = bounded_int(payload.get("redfin_rental_estimate_limit_per_zip"), 0, 0, 10000)
    redfin_rental_estimate_delay_seconds = bounded_float(payload.get("redfin_rental_estimate_delay_seconds"), 0.25, 0, 10)
    clear_profile_cache = bool(payload.get("clear_profile_cache"))
    zip_eligibility_filter = "hud_usps" if payload.get("zip_eligibility_filter", "hud_usps") != "none" else "none"
    cooldown_hours = bounded_float(payload.get("cooldown_hours"), 36, 0, 168)
    debug_snapshots = bool(payload.get("debug_snapshots", True))
    zillow_property_details = bool(payload.get("zillow_property_details", False))
    zillow_property_details_limit_per_zip = bounded_int(payload.get("zillow_property_details_limit_per_zip"), 25, 0, 500)
    zillow_property_details_delay_seconds = bounded_float(payload.get("zillow_property_details_delay_seconds"), 2.0, 0, 20)
    zillow_property_details_cooldown_hours = bounded_float(payload.get("zillow_property_details_cooldown_hours"), 36.0, 0, 168)
    zillow_property_details_force = bool(payload.get("zillow_property_details_force", False))
    realtor_zip_delay_seconds = bounded_float(payload.get("realtor_zip_delay_seconds"), 0.0, 0, 300)
    realtor_zip_budget = bounded_int(payload.get("realtor_zip_budget"), 0, 0, 5000)
    realtor_fresh_profile = bool(payload.get("realtor_fresh_profile", True))
    max_consecutive_blocks = bounded_int(payload.get("max_consecutive_blocks"), 3, 0, 20)
    chrome_path = str(payload.get("chrome_path") or DEFAULT_CHROME_PATH).strip()
    chromedriver_path = _resolve_chromedriver_path(
        chrome_path=chrome_path,
        chromedriver_path=str(payload.get("chromedriver_path") or "").strip(),
    )
    start_after_zip = _resolve_auto_resume_zip(payload, refresh_provider="all")

    command = [
        sys.executable,
        "-m",
        "re_analyzer.scrapers.side_by_side_scraper_runner",
        "--florida-refresh",
        "--providers",
        "zillow",
        "redfin",
        "realtor",
    ]
    if zillow_property_details:
        command.extend([
            "--zillow-property-details",
            "--zillow-property-details-limit-per-zip",
            str(zillow_property_details_limit_per_zip),
            "--zillow-property-details-delay-seconds",
            str(zillow_property_details_delay_seconds),
            "--zillow-property-details-cooldown-hours",
            str(zillow_property_details_cooldown_hours),
        ])
        if zillow_property_details_force:
            command.append("--zillow-property-details-force")
    command.extend([
        "--max-zip-codes",
        str(max_zip_codes),
        "--max-pages",
        str(max_pages),
        "--sample-size",
        str(sample_size),
        "--startup-stagger-seconds",
        str(startup_stagger_seconds),
        "--min-delay-seconds",
        str(min_delay_seconds),
        "--max-delay-seconds",
        str(max_delay_seconds),
        "--zip-delay-seconds",
        str(zip_delay_seconds),
        "--manual-challenge-wait-seconds",
        str(manual_wait_seconds),
        "--session-warmup-seconds",
        str(session_warmup_seconds),
        "--zip-navigation-warmup-seconds",
        str(zip_navigation_warmup_seconds),
        "--redfin-rental-estimate-limit-per-zip",
        str(redfin_rental_estimate_limit_per_zip),
        "--redfin-rental-estimate-delay-seconds",
        str(redfin_rental_estimate_delay_seconds),
        "--chrome-profile-directory",
        "Default",
        "--zip-eligibility-filter",
        zip_eligibility_filter,
        "--chrome-path",
        chrome_path,
    ])
    if chromedriver_path:
        command.extend(["--chromedriver-path", chromedriver_path])
    command.append("--redfin-rental-estimates" if redfin_rental_estimates else "--no-redfin-rental-estimates")
    if payload.get("all_pages", True):
        command.extend([
            "--all-pages",
            "--max-discovered-pages-per-zip",
            str(max_discovered_pages),
        ])
    if payload.get("respect_cooldown", True):
        command.extend([
            "--respect-cooldown",
            "--cooldown-hours",
            str(cooldown_hours),
        ])
    if start_after_zip:
        if not re.fullmatch(r"\d{5}", start_after_zip):
            raise ValueError("Resume ZIP must be exactly five digits.")
        command.extend(["--start-after-zip", start_after_zip])
    if realtor_zip_delay_seconds > 0:
        command.extend(["--realtor-zip-delay-seconds", str(realtor_zip_delay_seconds)])
    if realtor_zip_budget > 0:
        command.extend(["--realtor-zip-budget", str(realtor_zip_budget)])
    if realtor_fresh_profile:
        command.append("--realtor-fresh-profile")
    else:
        command.append("--no-realtor-fresh-profile")
    if max_consecutive_blocks > 0:
        command.extend(["--max-consecutive-blocks", str(max_consecutive_blocks)])
    if not debug_snapshots:
        command.append("--no-debug-snapshots")
    if payload.get("reconcile_debug_screenshots", True):
        command.append("--reconcile-debug-screenshots")
    else:
        command.append("--no-reconcile-debug-screenshots")
    if payload.get("save_results"):
        command.append("--save")
    if clear_profile_cache:
        command.append("--clear-profile-cache")

    return command, {
        "run_mode": "provider_florida_all",
        "refresh_provider": "all",
        "providers": ["zillow", "redfin", "realtor"],
        "zillow_property_details": zillow_property_details,
        "zillow_property_details_limit_per_zip": zillow_property_details_limit_per_zip,
        "zillow_property_details_delay_seconds": zillow_property_details_delay_seconds,
        "zillow_property_details_cooldown_hours": zillow_property_details_cooldown_hours,
        "zillow_property_details_force": zillow_property_details_force,
        "max_zip_codes": max_zip_codes,
        "all_pages": bool(payload.get("all_pages", True)),
        "max_pages": max_pages,
        "max_discovered_pages_per_zip": max_discovered_pages,
        "sample_size": sample_size,
        "startup_stagger_seconds": startup_stagger_seconds,
        "min_delay_seconds": min_delay_seconds,
        "max_delay_seconds": max_delay_seconds,
        "zip_delay_seconds": zip_delay_seconds,
        "manual_challenge_wait_seconds": manual_wait_seconds,
        "session_warmup_seconds": session_warmup_seconds,
        "zip_navigation_warmup_seconds": zip_navigation_warmup_seconds,
        "redfin_rental_estimates": redfin_rental_estimates,
        "redfin_rental_estimate_limit_per_zip": redfin_rental_estimate_limit_per_zip,
        "redfin_rental_estimate_delay_seconds": redfin_rental_estimate_delay_seconds,
        "clear_profile_cache": clear_profile_cache,
        "zip_eligibility_filter": zip_eligibility_filter,
        "stop_on_challenge": False,
        "respect_cooldown": bool(payload.get("respect_cooldown", True)),
        "cooldown_hours": cooldown_hours,
        "start_after_zip": start_after_zip,
        "debug_snapshots": debug_snapshots,
        "reconcile_debug_screenshots": bool(payload.get("reconcile_debug_screenshots", True)),
        "save_results": bool(payload.get("save_results")),
        "chrome_path": chrome_path,
        "chromedriver_path": chromedriver_path,
        "realtor_zip_delay_seconds": realtor_zip_delay_seconds,
        "realtor_zip_budget": realtor_zip_budget,
        "realtor_fresh_profile": realtor_fresh_profile,
        "max_consecutive_blocks": max_consecutive_blocks,
    }


def watch_process(run_id, process):
    lines = []
    with RUN_LOCK:
        run = RUNS.get(run_id)
        if run:
            run["status"] = "running"
            run["started_at"] = now_iso()
    try:
        for line in iter(process.stdout.readline, ""):
            clean_line = line.rstrip()
            lines.append(clean_line)
            with RUN_LOCK:
                run = RUNS.get(run_id)
                if run:
                    append_log(run, clean_line)
        return_code = process.wait()
        completed_at = datetime.now(timezone.utc)
        with RUN_LOCK:
            run = RUNS.get(run_id)
            if run:
                started_at = datetime.fromisoformat(run["started_at"]) if run.get("started_at") else completed_at
                run["completed_at"] = completed_at.isoformat()
                run["duration_seconds"] = round((completed_at - started_at).total_seconds(), 2)
                run["return_code"] = return_code
                run["summary"] = parse_summary(lines)
                if run.get("status") != "cancelled":
                    run["status"] = "completed" if return_code == 0 else "failed"
    except Exception as exc:
        with RUN_LOCK:
            run = RUNS.get(run_id)
            if run:
                run["status"] = "failed"
                run["error"] = str(exc)
                run["completed_at"] = now_iso()


def watch_probe_process(run_id, process):
    lines = []
    with PROBE_RUN_LOCK:
        run = PROBE_RUNS.get(run_id)
        if run:
            run["status"] = "running"
            run["started_at"] = now_iso()
    try:
        for line in iter(process.stdout.readline, ""):
            clean_line = line.rstrip()
            lines.append(clean_line)
            with PROBE_RUN_LOCK:
                run = PROBE_RUNS.get(run_id)
                if run:
                    append_probe_log(run, clean_line)
        return_code = process.wait()
        completed_at = datetime.now(timezone.utc)
        with PROBE_RUN_LOCK:
            run = PROBE_RUNS.get(run_id)
            if run:
                started_at = datetime.fromisoformat(run["started_at"]) if run.get("started_at") else completed_at
                run["completed_at"] = completed_at.isoformat()
                run["duration_seconds"] = round((completed_at - started_at).total_seconds(), 2)
                run["return_code"] = return_code
                run["summary"] = parse_summary(lines)
                if run.get("status") != "cancelled":
                    run["status"] = "completed" if return_code == 0 else "failed"
    except Exception as exc:
        with PROBE_RUN_LOCK:
            run = PROBE_RUNS.get(run_id)
            if run:
                run["status"] = "failed"
                run["error"] = str(exc)
                run["completed_at"] = now_iso()


@app.route("/api/health", methods=["GET"])
def health():
    binaries = {}
    try:
        binaries = browser_binaries_snapshot() or {}
    except Exception:
        binaries = {}
    with RESUME_PROGRESS_LOCK:
        progress = _load_resume_progress()
    by_provider = {
        key: (value or {}).get("zip_code")
        for key, value in (progress.get("by_refresh_provider") or {}).items()
    }
    return jsonify({
        "status": "ok",
        "enabled": True,
        "active_runs": active_count(),
        "active_probe_runs": active_probe_count(),
        "tracked_runs": len(RUNS),
        "tracked_probe_runs": len(PROBE_RUNS),
        "cwd": str(Path.cwd()),
        "python": sys.executable,
        "chrome_path": DEFAULT_CHROME_PATH,
        "chromedriver_path": binaries.get("chromedriver_path") or "",
        "browser_binaries": {
            "chrome": binaries.get("chrome") or [],
            "chromedriver": binaries.get("chromedriver") or [],
        },
        "log_limit": LOG_LIMIT,
        "resume_zip_codes": by_provider,
        "probe_root": str(PROBE_ROOT),
    })


def _strip_exc_stacktrace(text: str) -> str:
    """Strip Selenium/Python stacktrace boilerplate so only the exception message is kept."""
    for marker in (" Stacktrace:", "\nStacktrace:", "\nStack trace:", "\nTraceback (most"):
        idx = text.find(marker)
        if idx != -1:
            return text[:idx].strip()
    return text.strip()


@app.route("/api/zip-coverage", methods=["GET"])
def zip_coverage():
    try:
        cooldown_hours = float(request.args.get("cooldown_hours", 36))
    except (TypeError, ValueError):
        cooldown_hours = 36.0
    include_detail = request.args.get("detail", "0") not in ("0", "false", "")
    now = datetime.now(timezone.utc)
    re_root = Path(__file__).resolve().parents[1]

    # Load Florida ZIP list
    zip_path = re_root / "data" / "florida_zip_codes.txt"
    all_zips = []
    if zip_path.exists():
        content = zip_path.read_text(encoding="utf-8")
        all_zips = sorted(set(z.strip() for z in re.split(r"[,\s]+", content) if re.fullmatch(r"\d{5}", z.strip())))

    # Load HUD-USPS eligibility
    eligibility: dict = {}
    elig_path = re_root / "Data" / "Fetched" / "ZipEligibility" / "hud_usps_zip_eligibility.json"
    if elig_path.exists():
        try:
            eligibility = json.loads(elig_path.read_text(encoding="utf-8")).get("entries", {})
        except Exception:
            pass

    providers_list = ["zillow", "redfin", "realtor"]
    # zip_detail_map holds per-zip per-provider detail for the detail response
    zip_detail_map: dict = {z: {"zip_code": z, "eligible": eligibility.get(z, {}).get("eligible", True)} for z in all_zips}
    zip_provider_freshness: dict = {z: {} for z in all_zips}
    provider_summary: dict = {}

    for provider in providers_list:
        meta_dir = re_root / "Data" / "Fetched" / provider / "Metadata"
        fresh = stale = never = with_data = 0
        status_counts: dict = {}

        for zip_code in all_zips:
            meta_path = meta_dir / f"{zip_code}_metadata.json"
            if not meta_path.exists():
                never += 1
                zip_provider_freshness[zip_code][provider] = "never"
                if include_detail:
                    zip_detail_map[zip_code][provider] = {"freshness": "never"}
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                never += 1
                zip_provider_freshness[zip_code][provider] = "never"
                if include_detail:
                    zip_detail_map[zip_code][provider] = {"freshness": "never"}
                continue

            last_checked_str = meta.get("last_checked") or ""
            last_status = meta.get("last_status", "unknown")
            canonical_count = int(meta.get("canonical_count") or 0)
            raw_count = int(meta.get("raw_count") or 0)
            has_data = bool(meta.get("has_saved_payload"))
            dominant_zip = meta.get("dominant_listing_zip_code") or ""
            warning_count = int(meta.get("last_run", {}).get("warning_count") or len(meta.get("warnings") or []))
            error_count = int(meta.get("last_run", {}).get("error_count") or len(meta.get("errors") or []))

            status_counts[last_status] = status_counts.get(last_status, 0) + 1
            if has_data:
                with_data += 1

            age_hours = None
            is_fresh = False
            if last_checked_str:
                try:
                    lc = datetime.fromisoformat(last_checked_str)
                    if lc.tzinfo is None:
                        lc = lc.replace(tzinfo=timezone.utc)
                    age_hours = round((now - lc).total_seconds() / 3600, 2)
                    is_fresh = age_hours <= cooldown_hours
                except Exception:
                    pass

            freshness = "fresh" if is_fresh else "stale"
            zip_provider_freshness[zip_code][provider] = freshness

            if include_detail:
                raw_errors = list(meta.get("errors") or [])
                raw_warnings = list(meta.get("warnings") or [])
                zip_detail_map[zip_code][provider] = {
                    "freshness": freshness,
                    "last_status": last_status,
                    "canonical_count": canonical_count,
                    "raw_count": raw_count,
                    "has_saved_payload": has_data,
                    "dominant_zip": dominant_zip if dominant_zip and dominant_zip != zip_code else None,
                    "age_hours": age_hours,
                    "warning_count": warning_count,
                    "error_count": error_count,
                    "errors": [_strip_exc_stacktrace(str(e))[:300] for e in raw_errors[:5]],
                    "warnings": [_strip_exc_stacktrace(str(w))[:300] for w in raw_warnings[:5]],
                }

            if is_fresh:
                fresh += 1
            else:
                stale += 1

        provider_summary[provider] = {
            "fresh": fresh,
            "stale": stale,
            "never": never,
            "with_data": with_data,
            "status_counts": status_counts,
        }

    eligible_zips = [z for z in all_zips if eligibility.get(z, {}).get("eligible", True)]
    all_fresh = sum(
        1 for z in eligible_zips
        if all(zip_provider_freshness[z].get(p) == "fresh" for p in providers_list)
    )
    any_fresh = sum(
        1 for z in eligible_zips
        if any(zip_provider_freshness[z].get(p) == "fresh" for p in providers_list)
    )

    response: dict = {
        "total_zips": len(all_zips),
        "eligible_zips": len(eligible_zips),
        "cooldown_hours": cooldown_hours,
        "providers": provider_summary,
        "all_providers_fresh": all_fresh,
        "any_provider_fresh": any_fresh,
        "pending_eligible": len(eligible_zips) - all_fresh,
        "generated_at": now.isoformat(),
    }
    if include_detail:
        response["zip_detail"] = list(zip_detail_map.values())
    return jsonify(response)


@app.route("/api/probe-targets", methods=["GET"])
def probe_targets():
    try:
        from re_analyzer.scrapers.probe_targets import probe_targets as build_targets
        targets = build_targets()
    except Exception as exc:
        return jsonify({"error": str(exc), "targets": []}), 500
    return jsonify({"targets": targets})


@app.route("/api/scraper-runs", methods=["GET"])
def list_runs():
    with RUN_LOCK:
        runs = [public_run(run) for run in RUNS.values()]
    runs.sort(key=lambda item: item["created_at"], reverse=True)
    return jsonify({
        "enabled": True,
        "active_runs": active_count(),
        "runs": runs[:10],
    })


@app.route("/api/probe-runs", methods=["GET"])
def list_probe_runs():
    with PROBE_RUN_LOCK:
        runs = [public_run(run) for run in PROBE_RUNS.values()]
    runs.sort(key=lambda item: item["created_at"], reverse=True)
    return jsonify({
        "enabled": True,
        "active_runs": active_probe_count(),
        "runs": runs[:10],
    })


@app.route("/api/source-reconciliation/latest", methods=["GET"])
def source_reconciliation_latest():
    data, error = latest_reconciliation_report()
    if error:
        return jsonify(error), 404
    return jsonify(data)


@app.route("/api/scraper-runs", methods=["DELETE"])
def clear_runs():
    with RUN_LOCK:
        active_run_ids = [
            run_id for run_id, run in RUNS.items()
            if run.get("status") in {"starting", "running"}
        ]
        if active_run_ids:
            return jsonify({
                "error": "Stop active scraper sessions before clearing history.",
                "active_runs": len(active_run_ids),
            }), 409
        cleared = len(RUNS)
        RUNS.clear()
    return jsonify({
        "enabled": True,
        "active_runs": 0,
        "cleared": cleared,
        "runs": [],
    })


@app.route("/api/probe-runs", methods=["DELETE"])
def clear_probe_runs():
    with PROBE_RUN_LOCK:
        active_run_ids = [
            run_id for run_id, run in PROBE_RUNS.items()
            if run.get("status") in {"starting", "running"}
        ]
        if active_run_ids:
            return jsonify({
                "error": "Stop active probe runs before clearing history.",
                "active_runs": len(active_run_ids),
            }), 409
        cleared = len(PROBE_RUNS)
        PROBE_RUNS.clear()
    return jsonify({
        "enabled": True,
        "active_runs": 0,
        "cleared": cleared,
        "runs": [],
    })


@app.route("/api/scraper-runs", methods=["POST"])
def start_run():
    if active_total_count() >= 1:
        return jsonify({"error": "A scraper session is already running."}), 409

    payload = request.get_json(silent=True) or {}
    try:
        command, config = build_command(payload)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    if isinstance(payload, dict) and "chromedriver_path" in payload:
        try:
            requested = str(payload.get("chromedriver_path") or "").strip()
            if requested:
                config = dict(config or {})
                config["chromedriver_path"] = requested
        except Exception:
            pass

    run_id = uuid.uuid4().hex[:12]
    run = {
        "run_id": run_id,
        "status": "starting",
        "created_at": now_iso(),
        "config": config,
        "command_preview": " ".join(command[1:]),
        "logs": [],
        "summary": None,
        "error": None,
    }
    with RUN_LOCK:
        RUNS[run_id] = run

    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if isinstance(payload, dict) and "chromedriver_path" in payload:
            requested = str(payload.get("chromedriver_path") or "").strip()
            if requested:
                env["RE_ANALYZER_CHROMEDRIVER_PATH"] = requested
        process = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=(os.name != "nt"),
        )
        run["process"] = process
        thread = threading.Thread(target=watch_process, args=(run_id, process), daemon=True)
        run["thread"] = thread
        thread.start()
    except Exception as exc:
        with RUN_LOCK:
            run["status"] = "failed"
            run["error"] = str(exc)
            run["completed_at"] = now_iso()
        return jsonify({"error": str(exc), "run": public_run(run)}), 500

    return jsonify(public_run(run)), 202


@app.route("/api/probe-runs", methods=["POST"])
def start_probe_run():
    if active_total_count() >= 1:
        return jsonify({"error": "A scraper or probe session is already running."}), 409

    payload = request.get_json(silent=True) or {}
    manual_wait_seconds = bounded_float(payload.get("manual_challenge_wait_seconds"), 0, 0, 300)

    run_id = uuid.uuid4().hex[:12]
    try:
        command, config = build_probe_command(payload, run_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    if isinstance(payload, dict) and "chromedriver_path" in payload:
        try:
            requested = str(payload.get("chromedriver_path") or "").strip()
            if requested:
                config = dict(config or {})
                config["chromedriver_path"] = requested
        except Exception:
            pass

    run = {
        "kind": "probe",
        "run_id": run_id,
        "status": "starting",
        "created_at": now_iso(),
        "config": config,
        "command_preview": " ".join(command[1:]),
        "logs": [],
        "summary": None,
        "error": None,
    }
    with PROBE_RUN_LOCK:
        PROBE_RUNS[run_id] = run

    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["RE_ANALYZER_PROBE_MANUAL_CHALLENGE_WAIT_SECONDS"] = str(manual_wait_seconds)
        if isinstance(payload, dict) and "chromedriver_path" in payload:
            requested = str(payload.get("chromedriver_path") or "").strip()
            if requested:
                env["RE_ANALYZER_CHROMEDRIVER_PATH"] = requested
        process = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=(os.name != "nt"),
        )
        run["process"] = process
        thread = threading.Thread(target=watch_probe_process, args=(run_id, process), daemon=True)
        run["thread"] = thread
        thread.start()
    except Exception as exc:
        with PROBE_RUN_LOCK:
            run["status"] = "failed"
            run["error"] = str(exc)
            run["completed_at"] = now_iso()
        return jsonify({"error": str(exc), "run": public_run(run)}), 500

    return jsonify(public_run(run)), 202


@app.route("/api/scraper-runs/<run_id>", methods=["GET"])
def get_run(run_id):
    with RUN_LOCK:
        run = RUNS.get(run_id)
        if not run:
            return jsonify({"error": "Scraper run not found."}), 404
        return jsonify(public_run(run))


@app.route("/api/probe-runs/<run_id>", methods=["GET"])
def get_probe_run(run_id):
    with PROBE_RUN_LOCK:
        run = PROBE_RUNS.get(run_id)
        if not run:
            return jsonify({"error": "Probe run not found."}), 404
        return jsonify(public_run(run))


@app.route("/api/scraper-runs/<run_id>/cancel", methods=["POST"])
def cancel_run(run_id):
    with RUN_LOCK:
        run = RUNS.get(run_id)
        if not run:
            return jsonify({"error": "Scraper run not found."}), 404
        process = run.get("process")
    if not process or process.poll() is not None:
        return jsonify({"error": "Scraper run is not active."}), 409
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    with RUN_LOCK:
        run["status"] = "cancelled"
        run["completed_at"] = now_iso()
    return jsonify(public_run(run))


@app.route("/api/probe-runs/<run_id>/cancel", methods=["POST"])
def cancel_probe_run(run_id):
    with PROBE_RUN_LOCK:
        run = PROBE_RUNS.get(run_id)
        if not run:
            return jsonify({"error": "Probe run not found."}), 404
        process = run.get("process")
    if not process or process.poll() is not None:
        return jsonify({"error": "Probe run is not active."}), 409
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    with PROBE_RUN_LOCK:
        run["status"] = "cancelled"
        run["completed_at"] = now_iso()
    return jsonify(public_run(run))


@app.route("/api/test-sms-alert", methods=["POST"])
def test_sms_alert():
    from re_analyzer.scrapers.scraping_utility import send_sms_alert
    sent = send_sms_alert("[scraper-dashboard] test alert")
    if sent:
        return jsonify({"sent": True, "message": "SMS alert sent."})
    return jsonify({"sent": False, "message": "No credentials configured. Set RE_ANALYZER_ALERT_GMAIL (or GMAIL_MAIL_USERNAME) and RE_ANALYZER_ALERT_APP_PASSWORD (or GMAIL_MAIL_APP_PASSWORD) in the environment."}), 422


@app.route("/api/stale-zip-count", methods=["GET"])
def stale_zip_count():
    """
    Returns per-provider stale/fresh/never/total ZIP counts for a given freshness window.
    Query params: provider (zillow|redfin|realtor|all), cooldown_hours (float, default 36)
    """
    try:
        cooldown_hours = float(request.args.get("cooldown_hours", 36))
    except (TypeError, ValueError):
        cooldown_hours = 36.0

    provider_filter = (request.args.get("provider") or "all").strip().lower()
    now = datetime.now(timezone.utc)
    re_root = Path(__file__).resolve().parents[1]

    zip_path = re_root / "data" / "florida_zip_codes.txt"
    all_zips: list = []
    if zip_path.exists():
        content = zip_path.read_text(encoding="utf-8")
        all_zips = sorted(set(z.strip() for z in re.split(r"[,\s]+", content) if re.fullmatch(r"\d{5}", z.strip())))

    providers_to_check = (
        ["zillow", "redfin", "realtor"] if provider_filter == "all"
        else [p for p in ["zillow", "redfin", "realtor"] if p == provider_filter]
    )

    result: dict = {}
    for provider in providers_to_check:
        meta_dir = re_root / "Data" / "Fetched" / provider / "Metadata"
        stale = fresh = never = 0
        for zip_code in all_zips:
            meta_path = meta_dir / f"{zip_code}_metadata.json"
            if not meta_path.exists():
                never += 1
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                never += 1
                continue
            last_checked_str = meta.get("last_checked") or ""
            if not last_checked_str:
                never += 1
                continue
            try:
                lc = datetime.fromisoformat(last_checked_str)
                if lc.tzinfo is None:
                    lc = lc.replace(tzinfo=timezone.utc)
                age_hours = (now - lc).total_seconds() / 3600
                if age_hours <= cooldown_hours:
                    fresh += 1
                else:
                    stale += 1
            except Exception:
                never += 1
        result[provider] = {"stale": stale, "fresh": fresh, "never": never, "total": len(all_zips)}

    if provider_filter != "all" and providers_to_check:
        return jsonify(result.get(providers_to_check[0], {}))

    # Aggregate across all providers
    agg = {"stale": 0, "fresh": 0, "never": 0, "total": len(all_zips)}
    for v in result.values():
        agg["stale"] += v["stale"]
        agg["fresh"] += v["fresh"]
        agg["never"] += v["never"]
    return jsonify({"providers": result, "aggregate": agg})


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Local host scraper control server for the admin dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    # Note: 5061 is blocked by Chrome as an "unsafe port" (SIP).
    parser.add_argument("--port", type=int, default=5071)
    parser.add_argument("--chrome-path", default=DEFAULT_CHROME_PATH)
    parser.add_argument(
        "--allow-unauthenticated-network",
        action="store_true",
        help="Allow binding to a non-loopback interface without LOCAL_SCRAPER_CONTROL_TOKEN. Intended only for trusted local networks.",
    )
    return parser.parse_args(argv)


def main():
    args = parse_args()
    global DEFAULT_CHROME_PATH
    DEFAULT_CHROME_PATH = args.chrome_path
    if network_bind_requires_token(args.host) and not CONTROL_TOKEN and not args.allow_unauthenticated_network:
        raise SystemExit(
            "Refusing to bind the scraper controller to a network interface without "
            "LOCAL_SCRAPER_CONTROL_TOKEN. Use --host 127.0.0.1, set a token, or pass "
            "--allow-unauthenticated-network for trusted local debugging."
        )
    print(f"Local scraper controller listening on http://{args.host}:{args.port}")
    if CONTROL_TOKEN:
        print("Local scraper controller token authentication is enabled.")
    print("Open the admin dashboard or validation lab and use Local Scraper Sessions / Access Probes.")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
