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
from re_analyzer.utility.utility import DATA_PATH


RUNS = {}
RUN_LOCK = threading.Lock()
PROBE_RUNS = {}
PROBE_RUN_LOCK = threading.Lock()
LOG_LIMIT = int(os.environ.get("LOCAL_SCRAPER_LOG_LIMIT", "2000"))
RE_ANALYZER_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(DATA_PATH)
FETCHED_ROOT = DATA_ROOT / "Fetched"


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
PROFILE_ROOT = DATA_ROOT / "ScraperDiagnostics" / "ParallelProfiles"
RESUME_PROGRESS_PATH = DATA_ROOT / "ScraperDiagnostics" / "resume_progress.json"
DIAGNOSTICS_ROOT = DATA_ROOT / "ScraperDiagnostics"
RESUME_PROGRESS_LOCK = threading.Lock()
PROBE_ROOT = DATA_ROOT / "DetectionProbe" / "ProbeRuns"
CONTROL_TOKEN = os.environ.get("LOCAL_SCRAPER_CONTROL_TOKEN", "").strip()
REQUIRE_TOKEN_FOR_ALL = os.environ.get("LOCAL_SCRAPER_REQUIRE_TOKEN_FOR_ALL", "false").strip().lower() not in {"0", "false", "no", "off"}
REQUIRE_TOKEN_FOR_MUTATIONS = os.environ.get("LOCAL_SCRAPER_REQUIRE_TOKEN_FOR_MUTATIONS", "false").strip().lower() not in {"0", "false", "no", "off"}


def _token_requirements_payload():
    return {
        "token_configured": bool(CONTROL_TOKEN),
        "require_token_for_all": bool(REQUIRE_TOKEN_FOR_ALL),
        "require_token_for_mutations": bool(REQUIRE_TOKEN_FOR_MUTATIONS),
    }

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
    mutating_method = request.method in {"POST", "DELETE", "PUT", "PATCH"}
    token_required = bool(REQUIRE_TOKEN_FOR_ALL) or (bool(REQUIRE_TOKEN_FOR_MUTATIONS) and mutating_method)
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
    report_dir = FETCHED_ROOT / "Reconciliation"
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
        meta_dir = FETCHED_ROOT / provider / "Metadata"
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
    realtor_zip_delay_seconds = bounded_float(payload.get("realtor_zip_delay_seconds"), 20.0, 0, 300) if provider == "realtor" else 0.0
    realtor_zip_budget = bounded_int(payload.get("realtor_zip_budget"), 0, 0, 5000) if provider == "realtor" else 0
    realtor_fresh_profile = bool(payload.get("realtor_fresh_profile", False)) if provider == "realtor" else False
    realtor_cookie_donor_profiles = [str(d) for d in (payload.get("realtor_cookie_donor_profiles") or []) if d] if provider == "realtor" else []
    realtor_property_estimates = bool(payload.get("realtor_property_estimates", False)) if provider == "realtor" else False
    realtor_property_estimates_limit_per_zip = bounded_int(payload.get("realtor_property_estimates_limit_per_zip"), 0, 0, 10000) if provider == "realtor" else 0
    realtor_property_estimates_delay_seconds = bounded_float(payload.get("realtor_property_estimates_delay_seconds"), 0.5, 0, 30) if provider == "realtor" else 0.5
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
    for donor_dir in realtor_cookie_donor_profiles:
        command.extend(["--realtor-cookie-donor-profile", donor_dir])
    if realtor_property_estimates:
        command.extend([
            "--realtor-property-estimates",
            "--realtor-property-estimates-limit-per-zip",
            str(realtor_property_estimates_limit_per_zip),
            "--realtor-property-estimates-delay-seconds",
            str(realtor_property_estimates_delay_seconds),
        ])
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
        "realtor_cookie_donor_profiles": realtor_cookie_donor_profiles,
        "realtor_property_estimates": realtor_property_estimates,
        "realtor_property_estimates_limit_per_zip": realtor_property_estimates_limit_per_zip,
        "realtor_property_estimates_delay_seconds": realtor_property_estimates_delay_seconds,
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
    realtor_zip_delay_seconds = bounded_float(payload.get("realtor_zip_delay_seconds"), 20.0, 0, 300)
    realtor_zip_budget = bounded_int(payload.get("realtor_zip_budget"), 0, 0, 5000)
    realtor_fresh_profile = bool(payload.get("realtor_fresh_profile", False))
    realtor_cookie_donor_profiles = [str(d) for d in (payload.get("realtor_cookie_donor_profiles") or []) if d]
    realtor_property_estimates = bool(payload.get("realtor_property_estimates", False))
    realtor_property_estimates_limit_per_zip = bounded_int(payload.get("realtor_property_estimates_limit_per_zip"), 0, 0, 10000)
    realtor_property_estimates_delay_seconds = bounded_float(payload.get("realtor_property_estimates_delay_seconds"), 0.5, 0, 30)
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
    for donor_dir in realtor_cookie_donor_profiles:
        command.extend(["--realtor-cookie-donor-profiles", donor_dir])
    if realtor_property_estimates:
        command.extend([
            "--realtor-property-estimates",
            "--realtor-property-estimates-limit-per-zip",
            str(realtor_property_estimates_limit_per_zip),
            "--realtor-property-estimates-delay-seconds",
            str(realtor_property_estimates_delay_seconds),
        ])
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
        "realtor_cookie_donor_profiles": realtor_cookie_donor_profiles,
        "realtor_property_estimates": realtor_property_estimates,
        "realtor_property_estimates_limit_per_zip": realtor_property_estimates_limit_per_zip,
        "realtor_property_estimates_delay_seconds": realtor_property_estimates_delay_seconds,
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
        "auth": _token_requirements_payload(),
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
    re_root = RE_ANALYZER_ROOT

    # Load Florida ZIP list
    zip_path = re_root / "data" / "florida_zip_codes.txt"
    all_zips = []
    if zip_path.exists():
        content = zip_path.read_text(encoding="utf-8")
        all_zips = sorted(set(z.strip() for z in re.split(r"[,\s]+", content) if re.fullmatch(r"\d{5}", z.strip())))

    # Load HUD-USPS eligibility
    eligibility: dict = {}
    elig_path = FETCHED_ROOT / "ZipEligibility" / "hud_usps_zip_eligibility.json"
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
        meta_dir = FETCHED_ROOT / provider / "Metadata"
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


_DATA_QUALITY_FIELDS = [
    "price_estimate", "rent_estimate", "beds", "baths",
    "living_area", "lot_size", "year_built", "home_type",
    "status", "latitude", "longitude", "provider_metadata",
]


@app.route("/api/data-quality", methods=["GET"])
def data_quality():
    """Return field-level completeness stats per provider from canonical listing files."""
    try:
        from re_analyzer.scrapers.source_reconciler import DEFAULT_PROVIDERS
        from re_analyzer.utility.utility import DATA_PATH

        fetched_root = Path(DATA_PATH) / "Fetched"
        stats = {}
        for provider in DEFAULT_PROVIDERS:
            provider_dir = fetched_root / provider
            total = 0
            field_counts = {f: 0 for f in _DATA_QUALITY_FIELDS}
            zip_count = 0
            if provider_dir.is_dir():
                for zip_dir in provider_dir.iterdir():
                    if not (zip_dir.is_dir() and zip_dir.name.isdigit()):
                        continue
                    files = sorted(zip_dir.glob("canonical_listings_*.json"), key=lambda p: p.stat().st_mtime)
                    if not files:
                        continue
                    zip_count += 1
                    try:
                        with open(files[-1], "r", encoding="utf-8") as fh:
                            listings = json.load(fh)
                        if not isinstance(listings, list):
                            continue
                        for listing in listings:
                            total += 1
                            for field in _DATA_QUALITY_FIELDS:
                                val = listing.get(field)
                                if val is not None and val != "" and val != 0 and val != {} and val != []:
                                    field_counts[field] += 1
                    except (OSError, json.JSONDecodeError):
                        continue
            stats[provider] = {
                "total_listings": total,
                "total_zips": zip_count,
                "field_completeness": {
                    f: round(field_counts[f] / total, 4) if total else 0
                    for f in _DATA_QUALITY_FIELDS
                },
                "field_counts": field_counts,
            }
        return jsonify({"status": "ok", "providers": stats, "fields": _DATA_QUALITY_FIELDS})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/source-reconciliation/latest", methods=["GET"])
def source_reconciliation_latest():
    data, error = latest_reconciliation_report()
    if error:
        return jsonify(error), 404
    return jsonify(data)


@app.route("/api/source-reconciliation/run", methods=["POST"])
def source_reconciliation_run():
    """Discover all scraped ZIPs, run reconciliation, save the report, and return it."""
    try:
        from re_analyzer.scrapers.source_reconciler import (
            DEFAULT_PROVIDERS,
            reconcile_sources,
            save_reconciliation_report,
        )
        from re_analyzer.utility.utility import DATA_PATH

        fetched_root = Path(DATA_PATH) / "Fetched"
        zip_set = set()
        for provider in DEFAULT_PROVIDERS:
            provider_dir = fetched_root / provider
            if provider_dir.is_dir():
                for entry in provider_dir.iterdir():
                    if entry.is_dir() and entry.name.isdigit():
                        if any(entry.glob("canonical_listings_*.json")):
                            zip_set.add(entry.name)

        if not zip_set:
            return jsonify({"error": "No scraped canonical listing data found. Run the scraper first."}), 404

        zip_codes = sorted(zip_set)
        report = reconcile_sources(zip_codes)
        saved = save_reconciliation_report(report)

        return jsonify({
            "status": "ok",
            "zip_codes_processed": len(zip_codes),
            "path": saved.get("json_path"),
            "report": report,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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
    re_root = RE_ANALYZER_ROOT

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
        meta_dir = FETCHED_ROOT / provider / "Metadata"
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


@app.route("/api/data-cleanup", methods=["GET"])
def data_cleanup_scan():
    """Scan local storage targets and return sizes — dry run, nothing deleted."""
    try:
        targets_raw = (request.args.get("targets") or "").strip()
        targets = [t.strip() for t in targets_raw.split(",") if t.strip()] or None
        older_than_days_raw = request.args.get("older_than_days")
        older_than_days = float(older_than_days_raw) if older_than_days_raw else None
    except (TypeError, ValueError):
        return jsonify({"error": "invalid parameters"}), 400
    try:
        from re_analyzer.scrapers.data_cleanup import scan as cleanup_scan
        results = cleanup_scan(targets, older_than_days=older_than_days)
        return jsonify({
            "dry_run": True,
            "total_bytes": sum(t.size_bytes for t in results.values()),
            "total_items": sum(t.item_count for t in results.values()),
            "targets": {
                name: {
                    "label": t.label,
                    "description": t.description,
                    "size_bytes": t.size_bytes,
                    "item_count": t.item_count,
                    "item_label": t.item_label,
                    "freshness_detail": [
                        {"provider": p, "zip_code": z, "age_days": round(a, 1)}
                        for p, z, a in t.freshness_detail
                    ],
                }
                for name, t in results.items()
            },
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/data-cleanup", methods=["POST"])
def data_cleanup_execute():
    """Execute cleanup for the specified targets. Requires control token when configured."""
    require = require_control_token()
    if require:
        return require
    payload = request.get_json(silent=True) or {}
    try:
        targets_raw = payload.get("targets") or []
        targets = [str(t) for t in targets_raw] if targets_raw else None
        older_than_days_raw = payload.get("older_than_days")
        older_than_days = float(older_than_days_raw) if older_than_days_raw is not None else None
    except (TypeError, ValueError):
        return jsonify({"error": "invalid parameters"}), 400
    try:
        from re_analyzer.scrapers.data_cleanup import (
            scan as cleanup_scan, execute_cleanup, ALL_TARGETS,
        )
        if targets:
            invalid = [t for t in targets if t not in ALL_TARGETS]
            if invalid:
                return jsonify({"error": f"unknown targets: {invalid}"}), 400
        results = cleanup_scan(targets, older_than_days=older_than_days)
        delete_summary = execute_cleanup(results)
        freed_bytes = sum(
            ds.get("bytes_freed") or 0
            for ds in delete_summary.values()
            if ds.get("bytes_freed") is not None
        )
        return jsonify({
            "freed_bytes": freed_bytes,
            "targets": {
                name: {
                    "deleted": ds["deleted"],
                    "errors": ds.get("errors", []),
                    "bytes_freed": ds.get("bytes_freed"),
                }
                for name, ds in delete_summary.items()
            },
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/profile-manager", methods=["GET"])
def profile_manager_scan():
    """Scan Chrome scraper profiles and return their health status."""
    try:
        from re_analyzer.scrapers.realtor_profile_manager import scan_profiles
        extra = [str(d) for d in (request.args.getlist("extra_dir") or []) if d]
        profiles = scan_profiles(extra_dirs=extra or None)
        return jsonify({"profiles": profiles, "count": len(profiles)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/profile-manager", methods=["POST"])
def profile_manager_apply():
    """Apply cleanup/seed operations to one or more Chrome scraper profiles."""
    try:
        require = require_control_token()
        if require:
            return require
        payload = request.get_json(force=True) or {}
        profile_subdir = str(payload.get("profile_subdir") or "Default")
        profile_targets = []
        for target in payload.get("profiles") or []:
            if isinstance(target, dict):
                profile_dir = str(target.get("profile_dir") or target.get("dir") or "")
                target_subdir = str(target.get("profile_subdir") or profile_subdir)
            else:
                profile_dir = str(target or "")
                target_subdir = profile_subdir
            if profile_dir:
                profile_targets.append((profile_dir, target_subdir))
        operations = [str(op) for op in (payload.get("operations") or []) if op]
        if not profile_targets:
            return jsonify({"error": "profiles list is required"}), 400
        if not operations:
            return jsonify({"error": "operations list is required"}), 400
        from re_analyzer.scrapers.realtor_profile_manager import apply_operations
        results = {}
        for profile_dir, target_subdir in profile_targets:
            key = f"{profile_dir}::{target_subdir}"
            results[key] = apply_operations(profile_dir, target_subdir, operations)
        return jsonify({
            "profiles_processed": len(results),
            "operations": operations,
            "results": results,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/profile-manager/donors", methods=["GET"])
def profile_manager_donors():
    """Scan the user's real Chrome installations for profiles with realtor.com cookies."""
    try:
        from re_analyzer.scrapers.realtor_profile_manager import find_donor_profiles
        include_empty = str(request.args.get("include_empty") or "").lower() in {"1", "true", "yes"}
        donors = find_donor_profiles(include_empty=include_empty)
        return jsonify({"donors": donors, "count": len(donors)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/profile-manager/copy-donor-cookies", methods=["POST"])
def profile_manager_copy_donor_cookies():
    """Copy realtor.com cookies from a donor browser profile to one or more scraper profiles."""
    try:
        require = require_control_token()
        if require:
            return require
        payload = request.get_json(force=True) or {}
        donor_profile_dir = str(payload.get("donor_profile_dir") or "")
        donor_profile_subdir = str(payload.get("donor_profile_subdir") or "Default")
        dest_profile_subdir = str(payload.get("dest_profile_subdir") or "Default")
        clear_dest_detection = bool(payload.get("clear_dest_detection", False))
        dest_targets = []
        for target in payload.get("dest_profiles") or []:
            if isinstance(target, dict):
                dest_dir = str(target.get("profile_dir") or target.get("dir") or "")
                target_subdir = str(target.get("profile_subdir") or dest_profile_subdir)
            else:
                dest_dir = str(target or "")
                target_subdir = dest_profile_subdir
            if dest_dir:
                dest_targets.append((dest_dir, target_subdir))
        skip_detection = bool(payload.get("skip_detection", True))
        if not donor_profile_dir:
            return jsonify({"error": "donor_profile_dir is required"}), 400
        if not dest_targets:
            return jsonify({"error": "dest_profiles list is required"}), 400
        from re_analyzer.scrapers.realtor_profile_manager import clear_detection_cookies, copy_donor_cookies
        results = {}
        for dest_dir, target_subdir in dest_targets:
            key = f"{dest_dir}::{target_subdir}"
            cleanup_result = None
            if clear_dest_detection:
                cleanup_result = clear_detection_cookies(dest_dir, target_subdir)
            copy_result = copy_donor_cookies(
                donor_profile_dir, donor_profile_subdir,
                str(dest_dir), target_subdir,
                skip_detection=skip_detection,
            )
            if cleanup_result is not None:
                copy_result["dest_detection_cleanup"] = cleanup_result
            results[key] = copy_result
        total_copied = sum(r.get("copied", 0) for r in results.values())
        total_dest_detection_removed = sum(
            (r.get("dest_detection_cleanup") or {}).get("removed", 0)
            for r in results.values()
        )
        return jsonify({
            "profiles_processed": len(results),
            "total_copied": total_copied,
            "total_dest_detection_removed": total_dest_detection_removed,
            "results": results,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _parse_diagnostic_stem(stem: str):
    """Parse '20260607_024516_realtor_32063_prepare_session_blocked' → dict or None."""
    parts = stem.split("_")
    if len(parts) < 4:
        return None
    try:
        d, t = parts[0], parts[1]
        timestamp = f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}"
        return {
            "timestamp": timestamp,
            "provider": parts[2],
            "zip_code": parts[3],
            "reason": "_".join(parts[4:]) if len(parts) > 4 else "unknown",
        }
    except (IndexError, ValueError):
        return None


def _recent_diagnostic_examples():
    manifest_path = DIAGNOSTICS_ROOT / "RecentExamples" / "manifest.json"
    manifest = {}
    if manifest_path.is_file():
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, json.JSONDecodeError):
            manifest = {}

    buckets = manifest.get("buckets") or {}
    if DIAGNOSTICS_ROOT.is_dir():
        raw_examples = {}
        for path in DIAGNOSTICS_ROOT.iterdir():
            if not path.is_file() or path.suffix.lower() not in {".json", ".html", ".png"}:
                continue
            parsed = _parse_diagnostic_stem(path.stem)
            if not parsed:
                continue
            prefix = "_".join(path.stem.split("_")[2:])
            item = raw_examples.setdefault(path.stem, {
                **parsed,
                "prefix": prefix,
                "captured_at_epoch": path.stat().st_mtime,
                "files": {},
                "source_files": {},
            })
            if path.suffix.lower() == ".json":
                label = "metadata"
            elif path.suffix.lower() == ".html":
                label = "html"
            else:
                label = "screenshot"
            item["files"][label] = path.name
            item["source_files"][label] = path.name
        for item in raw_examples.values():
            key = f"{item.get('provider')}/{item.get('reason')}"
            bucket = buckets.setdefault(key, {
                "provider": item.get("provider"),
                "reason": item.get("reason"),
                "examples": [],
            })
            seen = {(example.get("timestamp"), example.get("prefix")) for example in bucket.get("examples") or []}
            identity = (item.get("timestamp"), item.get("prefix"))
            if identity not in seen:
                bucket.setdefault("examples", []).append(item)

    limit = manifest.get("limit_per_provider_reason") or 3
    examples = []
    by_provider = {}
    by_reason = {}
    for key, bucket in buckets.items():
        bucket_examples = sorted(
            bucket.get("examples") or [],
            key=lambda item: (float(item.get("captured_at_epoch") or 0), item.get("timestamp", ""), item.get("prefix", "")),
            reverse=True,
        )
        retained_examples = bucket_examples[:limit]
        bucket["examples"] = retained_examples
        bucket["count"] = len(retained_examples)
        examples.extend(retained_examples)
        provider = bucket.get("provider") or key.split("/")[0]
        reason = bucket.get("reason") or key.split("/")[-1]
        by_provider[provider] = by_provider.get(provider, 0) + len(retained_examples)
        by_reason[reason] = by_reason.get(reason, 0) + len(retained_examples)
    examples.sort(
        key=lambda item: (float(item.get("captured_at_epoch") or 0), item.get("timestamp", ""), item.get("prefix", "")),
        reverse=True,
    )
    return {
        "updated_at": manifest.get("updated_at"),
        "limit_per_provider_reason": limit,
        "total": len(examples),
        "examples": examples,
        "buckets": buckets,
        "by_provider": by_provider,
        "by_reason": by_reason,
    }


@app.route("/api/block-events", methods=["GET"])
def block_events():
    """Return block/detection events parsed from ScraperDiagnostics JSON files."""
    try:
        limit = min(int(request.args.get("limit", "500")), 2000)
        provider_filter = (request.args.get("provider") or "").strip().lower()
        events = []
        if DIAGNOSTICS_ROOT.is_dir():
            paths = sorted(
                (p for p in DIAGNOSTICS_ROOT.iterdir() if p.suffix == ".json" and p.is_file()),
                key=lambda p: p.name,
                reverse=True,
            )
            for path in paths:
                parsed = _parse_diagnostic_stem(path.stem)
                if not parsed:
                    continue
                if provider_filter and parsed["provider"] != provider_filter:
                    continue
                event = dict(parsed, filename=path.name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        data = json.load(fh)
                    blocked = (data.get("extra") or {}).get("blocked") or {}
                    if blocked:
                        event["reference_id"] = str(blocked.get("reference_id") or "")
                        event["block_reason"] = str(blocked.get("reason") or "")
                        event["http_status"] = blocked.get("status")
                        event["snippet"] = str(blocked.get("snippet") or "")[:300]
                    challenge = (data.get("extra") or {}).get("challenge") or {}
                    event["is_challenge"] = bool(challenge.get("is_challenge"))
                    cookies = ((data.get("extra") or {}).get("page_state") or {}).get("cookies") or {}
                    event["cookie_count"] = cookies.get("count")
                except Exception:
                    pass
                events.append(event)
                if len(events) >= limit:
                    break

        by_provider: dict = {}
        by_reason: dict = {}
        for evt in events:
            p = evt["provider"]
            r = evt.get("block_reason") or evt.get("reason") or "unknown"
            by_provider[p] = by_provider.get(p, 0) + 1
            by_reason[r] = by_reason.get(r, 0) + 1

        return jsonify({"events": events, "total": len(events), "by_provider": by_provider, "by_reason": by_reason})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/diagnostics", methods=["GET"])
def list_diagnostics():
    """List all files under ScraperDiagnostics/ with parsed metadata."""
    try:
        provider_filter = (request.args.get("provider") or "").strip().lower()
        type_filter = (request.args.get("type") or "").strip().lower()
        files = []
        if DIAGNOSTICS_ROOT.is_dir():
            for path in sorted(DIAGNOSTICS_ROOT.iterdir(), key=lambda p: p.name, reverse=True):
                if not path.is_file():
                    continue
                parsed = _parse_diagnostic_stem(path.stem)
                if not parsed:
                    continue
                ftype = path.suffix.lstrip(".").lower()
                if provider_filter and parsed["provider"] != provider_filter:
                    continue
                if type_filter and ftype != type_filter:
                    continue
                files.append({"name": path.name, "type": ftype, "size": path.stat().st_size, **parsed})
        by_provider: dict = {}
        by_reason: dict = {}
        for f in files:
            by_provider[f["provider"]] = by_provider.get(f["provider"], 0) + 1
            by_reason[f["reason"]] = by_reason.get(f["reason"], 0) + 1
        return jsonify({
            "files": files,
            "total": len(files),
            "by_provider": by_provider,
            "by_reason": by_reason,
            "recent_examples": _recent_diagnostic_examples(),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/diagnostics/<path:filename>", methods=["GET"])
def get_diagnostic_file(filename):
    """Return the content of a diagnostic file (JSON/HTML/PNG)."""
    try:
        import base64 as _b64
        normalized_filename = str(filename or "").replace("\\", "/").lstrip("/")
        if ".." in normalized_filename.split("/"):
            return jsonify({"error": "Invalid filename"}), 400
        path = DIAGNOSTICS_ROOT / normalized_filename
        if not path.is_file():
            return jsonify({"error": "File not found"}), 404
        try:
            path.resolve().relative_to(DIAGNOSTICS_ROOT.resolve())
        except ValueError:
            return jsonify({"error": "Access denied"}), 403
        suffix = path.suffix.lower()
        if suffix == ".json":
            with open(path, encoding="utf-8") as fh:
                return jsonify({"type": "json", "content": json.load(fh), "size": path.stat().st_size})
        elif suffix == ".html":
            with open(path, encoding="utf-8", errors="replace") as fh:
                content = fh.read(60_000)
            return jsonify({"type": "html", "content": content, "size": path.stat().st_size})
        elif suffix == ".png":
            with open(path, "rb") as fh:
                raw = fh.read()
            return jsonify({"type": "png", "data": _b64.b64encode(raw).decode(), "size": path.stat().st_size})
        return jsonify({"error": "Unsupported file type"}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/resume-progress", methods=["GET"])
def get_resume_progress():
    """Return the current scraper resume-progress state."""
    try:
        if not RESUME_PROGRESS_PATH.is_file():
            return jsonify({"progress": {}, "exists": False})
        with open(RESUME_PROGRESS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return jsonify({"progress": data, "exists": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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
