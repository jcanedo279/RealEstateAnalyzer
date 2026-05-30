from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        try:
            return repr(value)
        except Exception:
            return ""


def _package_version(dist_name: str) -> str:
    try:
        return metadata.version(dist_name)
    except Exception:
        return ""


def redact_path(value: str) -> str:
    """
    Redact local absolute paths for easier sharing of diagnostics.

    - Replaces the current user's home directory with "~".
    - Leaves non-path strings unchanged.
    """
    text = _safe_str(value)
    if not text:
        return text

    try:
        home = str(Path.home())
    except Exception:
        home = ""

    candidates = {home}
    if home:
        candidates.add(home.replace("\\", "/"))
        candidates.add(home.replace("/", "\\"))

    for candidate in sorted({c for c in candidates if c and c != "/"} , key=len, reverse=True):
        text = text.replace(candidate, "~")
    return text


def redact_paths_in_args(args: Iterable[str]) -> list[str]:
    redacted: list[str] = []
    for raw in list(args or []):
        arg = _safe_str(raw)
        if not arg:
            continue
        if "=" in arg and arg.startswith("--"):
            flag, value = arg.split("=", 1)
            redacted.append(f"{flag}={redact_path(value)}")
        else:
            redacted.append(redact_path(arg))
    return redacted


def environment_versions() -> Dict[str, Any]:
    """
    Minimal environment + dependency versions for troubleshooting driver mismatches.
    """
    return {
        "python": _safe_str(sys.version).splitlines()[0],
        "platform": _safe_str(platform.platform()),
        "machine": _safe_str(platform.machine()),
        "selenium": _package_version("selenium"),
        "undetected_chromedriver": _package_version("undetected-chromedriver"),
    }


def _major_version(value: Any) -> str:
    match = re.search(r"\b(\d{2,4})\.", _safe_str(value))
    return match.group(1) if match else ""


def _version_tuple(value: Any) -> tuple[str, ...]:
    match = re.search(r"\b(\d{2,4}(?:\.\d+){1,3})\b", _safe_str(value))
    return tuple(match.group(1).split(".")) if match else tuple()


def _version_text(value: Any) -> str:
    parts = _version_tuple(value)
    return ".".join(parts) if parts else ""


def _build_text(value: Any) -> str:
    parts = _version_tuple(value)
    return ".".join(parts[:3]) if len(parts) >= 3 else ""


def _chrome_binary_version(binary_path: str) -> Dict[str, str]:
    path = _safe_str(binary_path).strip()
    if not path:
        return {"path": "", "version": "", "major": "", "error": "missing"}
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception as exc:
        return {"path": redact_path(path), "version": "", "major": "", "error": f"{type(exc).__name__}: {exc}"}
    output = (proc.stdout or proc.stderr or "").strip()
    return {
        "path": redact_path(path),
        "version": output,
        "version_number": _version_text(output),
        "build": _build_text(output),
        "major": _major_version(output),
        "error": "" if proc.returncode == 0 and output else f"exit={proc.returncode}",
    }


def _capabilities_dict(driver: Any) -> Dict[str, Any]:
    try:
        caps = getattr(driver, "capabilities", None)
        if isinstance(caps, dict):
            return caps
    except Exception:
        pass
    return {}


def summarize_capabilities(capabilities: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a stable subset of Selenium capabilities (no huge nested blobs).
    """
    caps = dict(capabilities or {})
    chrome = caps.get("chrome") if isinstance(caps.get("chrome"), dict) else {}

    chromedriver_raw = ""
    try:
        chromedriver_raw = _safe_str(chrome.get("chromedriverVersion", ""))
    except Exception:
        chromedriver_raw = ""
    chromedriver_version = chromedriver_raw.split(" ")[0] if chromedriver_raw else ""

    user_data_dir = ""
    try:
        user_data_dir = _safe_str(chrome.get("userDataDir", ""))
    except Exception:
        user_data_dir = ""

    browser_version = _safe_str(caps.get("browserVersion") or caps.get("version") or "")

    return {
        "browser_name": _safe_str(caps.get("browserName") or ""),
        "browser_version": browser_version,
        "browser_version_number": _version_text(browser_version),
        "browser_build": _build_text(browser_version),
        "platform_name": _safe_str(caps.get("platformName") or caps.get("platform") or ""),
        "accept_insecure_certs": bool(caps.get("acceptInsecureCerts")) if "acceptInsecureCerts" in caps else None,
        "page_load_strategy": _safe_str(caps.get("pageLoadStrategy") or ""),
        "timeouts": caps.get("timeouts") if isinstance(caps.get("timeouts"), dict) else {},
        "chromedriver": {
            "version": chromedriver_version,
            "version_number": _version_text(chromedriver_version),
            "build": _build_text(chromedriver_version),
            "raw": chromedriver_raw,
            "user_data_dir": redact_path(user_data_dir),
        },
        "cdp": {
            "endpoint": _safe_str(caps.get("se:cdp") or ""),
            "version": _safe_str(caps.get("se:cdpVersion") or ""),
        },
    }


def navigator_snapshot(driver: Any) -> Dict[str, Any]:
    """
    Capture a small navigator/viewport snapshot without attempting to modify it.
    """
    def _js(script: str, default: Any) -> Any:
        try:
            return driver.execute_script(script)
        except Exception:
            return default

    snapshot = {
        "user_agent": _safe_str(_js("return navigator.userAgent;", "")),
        "webdriver": _js("return navigator.webdriver;", None),
        "webdriver_descriptor": _js(
            """
            try {
              const proto = Object.getPrototypeOf(navigator);
              const dProto = Object.getOwnPropertyDescriptor(proto, 'webdriver') || null;
              const dSelf = Object.getOwnPropertyDescriptor(navigator, 'webdriver') || null;
              const pack = (d) => {
                if (!d) return null;
                return {
                  configurable: Boolean(d.configurable),
                  enumerable: Boolean(d.enumerable),
                  has_get: Boolean(d.get),
                  has_value: Object.prototype.hasOwnProperty.call(d, 'value'),
                  value: Object.prototype.hasOwnProperty.call(d, 'value') ? d.value : undefined,
                };
              };
              return { on_prototype: pack(dProto), on_instance: pack(dSelf) };
            } catch (e) {
              return { error: String(e) };
            }
            """,
            None,
        ),
        "languages": _js("return navigator.languages;", []) or [],
        "platform": _safe_str(_js("return navigator.platform;", "")),
        "timezone": _safe_str(_js("try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return ''; }", "")),
        "viewport": _js(
            "return { innerWidth: window.innerWidth, innerHeight: window.innerHeight, devicePixelRatio: window.devicePixelRatio };",
            {},
        )
        or {},
    }

    # userAgentData can throw on older builds; treat as optional.
    user_agent_data = _js(
        "try { return navigator.userAgentData ? { brands: navigator.userAgentData.brands, mobile: navigator.userAgentData.mobile, platform: navigator.userAgentData.platform } : null; } catch (e) { return null; }",
        None,
    )
    if user_agent_data:
        snapshot["user_agent_data"] = user_agent_data

    return snapshot


def _ua_major(user_agent: str) -> str:
    match = re.search(r"\b(?:HeadlessChrome|Chrome|Chromium)/(\d{2,4})\.", _safe_str(user_agent))
    return match.group(1) if match else ""


def _ua_ch_major(user_agent_data: Any) -> str:
    if not isinstance(user_agent_data, dict):
        return ""
    brands = user_agent_data.get("brands")
    if not isinstance(brands, list):
        return ""
    for brand in brands:
        if not isinstance(brand, dict):
            continue
        name = _safe_str(brand.get("brand"))
        if name in {"Google Chrome", "Chromium"}:
            return _safe_str(brand.get("version"))
    return ""


def _option_values(arguments: Iterable[str], flag: str) -> list[str]:
    values = []
    prefix = f"{flag}="
    for arg in list(arguments or []):
        text = _safe_str(arg)
        if text == flag:
            values.append("")
        elif text.startswith(prefix):
            values.append(text.split("=", 1)[1])
    return values


def browser_hygiene_snapshot(report: Dict[str, Any]) -> Dict[str, Any]:
    capabilities = report.get("capabilities") if isinstance(report.get("capabilities"), dict) else {}
    navigator = report.get("navigator") if isinstance(report.get("navigator"), dict) else {}
    options = report.get("options") if isinstance(report.get("options"), dict) else {}
    binary = report.get("browser_binary") if isinstance(report.get("browser_binary"), dict) else {}

    browser_major = _major_version(capabilities.get("browser_version"))
    browser_version = _safe_str(capabilities.get("browser_version_number") or capabilities.get("browser_version"))
    browser_build = _safe_str(capabilities.get("browser_build"))
    chromedriver = capabilities.get("chromedriver") if isinstance(capabilities.get("chromedriver"), dict) else {}
    chromedriver_version = _safe_str(chromedriver.get("version_number") or chromedriver.get("version"))
    chromedriver_build = _safe_str(chromedriver.get("build"))
    chromedriver_major = _major_version(chromedriver_version)
    binary_version = _safe_str(binary.get("version_number") or binary.get("version"))
    binary_build = _safe_str(binary.get("build"))
    binary_major = _safe_str(binary.get("major"))
    ua_major = _ua_major(navigator.get("user_agent"))
    ua_ch_major = _ua_ch_major(navigator.get("user_agent_data"))
    window_sizes = _option_values(options.get("arguments") or [], "--window-size")
    profile_dirs = _option_values(options.get("arguments") or [], "--profile-directory")
    cft_user_data = "chrome for testing" in _safe_str((report.get("driver_config") or {}).get("user_data_dir")).lower()

    return {
        "browser_major": browser_major,
        "browser_version": browser_version,
        "browser_build": browser_build,
        "chromedriver_major": chromedriver_major,
        "chromedriver_version": chromedriver_version,
        "chromedriver_build": chromedriver_build,
        "binary_major": binary_major,
        "binary_version": binary_version,
        "binary_build": binary_build,
        "ua_major": ua_major,
        "ua_ch_major": ua_ch_major,
        "binary_browser_major_match": bool(binary_major and browser_major and binary_major == browser_major),
        "binary_browser_version_match": bool(binary_version and browser_version and binary_version == browser_version),
        "chromedriver_browser_major_match": bool(chromedriver_major and browser_major and chromedriver_major == browser_major),
        "chromedriver_browser_build_match": bool(chromedriver_build and browser_build and chromedriver_build == browser_build),
        "chromedriver_browser_version_match": bool(chromedriver_version and browser_version and chromedriver_version == browser_version),
        "ua_browser_major_match": bool(ua_major and browser_major and ua_major == browser_major),
        "ua_ch_browser_major_match": bool((not ua_ch_major) or (browser_major and ua_ch_major == browser_major)),
        "headless_user_agent": "HeadlessChrome/" in _safe_str(navigator.get("user_agent")),
        "window_size_values": window_sizes,
        "duplicate_window_size_args": len(set(window_sizes)) > 1,
        "profile_directory_values": profile_dirs,
        "chrome_for_testing_uses_rotated_profile": bool(cft_user_data and any(re.fullmatch(r"Profile\s+\d+", value or "") for value in profile_dirs)),
    }


def driver_config_snapshot(driver_config: Any) -> Dict[str, Any]:
    if driver_config is None:
        return {}
    if is_dataclass(driver_config):
        raw = asdict(driver_config)
    else:
        raw = {}
        for key in (
            "browser_executable_path",
            "chromedriver_executable_path",
            "user_data_dir",
            "profile_directory",
            "ignore_detection",
            "headless",
            "random_profile",
            "clean_profile",
            "window_rect",
            "enforce_window_rect",
            "allow_insecure_certs",
            "user_multi_procs",
            "startup_lock_mode",
        ):
            try:
                raw[key] = getattr(driver_config, key)
            except Exception:
                continue
    for key in ("browser_executable_path", "chromedriver_executable_path", "user_data_dir", "profile_directory"):
        if key in raw:
            raw[key] = redact_path(_safe_str(raw.get(key)))
    return raw


def options_snapshot(options: Any) -> Dict[str, Any]:
    if options is None:
        return {}
    args = []
    try:
        args = list(getattr(options, "arguments", []) or [])
    except Exception:
        args = []
    out: Dict[str, Any] = {"arguments": redact_paths_in_args(args)}
    try:
        experimental = getattr(options, "experimental_options", None)
        if isinstance(experimental, dict):
            out["experimental_option_keys"] = sorted(experimental.keys())
    except Exception:
        pass
    try:
        caps = getattr(options, "to_capabilities", None)
        if callable(caps):
            rendered = options.to_capabilities()
            out["capabilities_keys"] = sorted(rendered.keys()) if isinstance(rendered, dict) else []
    except Exception:
        pass
    return out


def collect_browser_report(
    *,
    driver: Any,
    driver_config: Any = None,
    options: Any = None,
) -> Dict[str, Any]:
    """
    Collect a shareable snapshot of driver/browser settings + versions.

    This is intended for debugging and sanity checks. It does not attempt to
    bypass or modify any anti-bot challenges.
    """
    if driver_config is None:
        try:
            driver_config = getattr(driver, "_re_analyzer_driver_config", None)
        except Exception:
            driver_config = None
    if options is None:
        try:
            options = getattr(driver, "_re_analyzer_options", None)
        except Exception:
            options = None

    caps = _capabilities_dict(driver)
    browser_binary = {}
    try:
        browser_binary = _chrome_binary_version(getattr(driver_config, "browser_executable_path", "") if driver_config else "")
    except Exception:
        browser_binary = {}
    report = {
        "versions": environment_versions(),
        "browser_binary": browser_binary,
        "driver_config": driver_config_snapshot(driver_config),
        "options": options_snapshot(options),
        "capabilities": summarize_capabilities(caps),
        "navigator": navigator_snapshot(driver),
    }
    report["hygiene"] = browser_hygiene_snapshot(report)
    return report


def report_as_pretty_json(report: Dict[str, Any]) -> str:
    try:
        return json.dumps(report, indent=2, sort_keys=True)
    except Exception:
        return _safe_str(report)
