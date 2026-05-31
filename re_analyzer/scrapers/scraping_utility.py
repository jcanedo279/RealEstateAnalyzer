import os
import shutil
import time
import json
import re
import glob
import platform
import subprocess
import random as rd
import tempfile
from pathlib import Path
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from dataclasses import dataclass
from functools import wraps
from contextlib import contextmanager
from typing import Dict, Optional
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from collections import defaultdict

from re_analyzer.utility.utility import DEFAULT_DATA_PATH, PROJECT_CONFIG, DATA_PATH, SEARCH_LISTINGS_METADATA_PATH, random_delay


# Chromium versions found at: https://vikyd.github.io/download-chromium-history-version/#/
def _bundled_chrome_for_testing_binary() -> str:
    """
    Prefer the repo-bundled "Chrome for Testing" build when available.

    This gives us a more controlled, repeatable browser environment for probe
    runs and manual access-pattern debugging.
    """
    try:
        re_analyzer_root = Path(__file__).resolve().parents[1]
    except Exception:
        return ""

    candidate = (
        re_analyzer_root
        / "ChromeAssets"
        / "Google Chrome for Testing.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome for Testing"
    )
    try:
        return str(candidate) if candidate.exists() else ""
    except Exception:
        return ""


def _bundled_chromedriver_binary() -> str:
    """
    Prefer a repo-managed chromedriver when present.

    Puppeteer's browser installer usually places chromedriver under:
    ChromeAssets/.downloads/chromedriver/<platform-version>/chromedriver-*/chromedriver
    We also support a flat ChromeAssets/chromedriver path for manual installs.
    """
    try:
        chrome_assets = Path(__file__).resolve().parents[1] / "ChromeAssets"
    except Exception:
        return ""

    candidates = [
        chrome_assets / "chromedriver",
    ]
    try:
        candidates.extend(sorted((chrome_assets / ".downloads").glob("chromedriver/*/chromedriver*/chromedriver")))
    except Exception:
        pass

    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return str(candidate)
        except Exception:
            continue
    return ""


def _detect_chromedriver_major_version(chromedriver_path: str) -> int | None:
    """
    Extract the ChromeDriver major version from `chromedriver --version`.
    """
    path = str(chromedriver_path or "").strip()
    if not path:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    text = (proc.stdout or proc.stderr or "").strip()
    match = re.search(r"\bChromeDriver\s+(\d{2,4})\.", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _pick_bundled_chromedriver_binary(*, preferred_major: int | None = None) -> str:
    """
    Pick the best available chromedriver under ChromeAssets.

    - Prefer an exact major match when `preferred_major` is provided.
    - Otherwise pick the highest major version we can identify.
    """
    try:
        chrome_assets = Path(__file__).resolve().parents[1] / "ChromeAssets"
    except Exception:
        return ""

    candidates: list[Path] = [chrome_assets / "chromedriver"]
    try:
        candidates.extend(sorted((chrome_assets / ".downloads").glob("chromedriver/*/chromedriver*/chromedriver")))
    except Exception:
        pass

    scored: list[tuple[int, int, str]] = []
    for candidate in candidates:
        try:
            if not candidate.exists() or not candidate.is_file():
                continue
            path = str(candidate)
            major = _detect_chromedriver_major_version(path) or -1
            # Prefer explicit major match when requested; otherwise just sort by major.
            match_bonus = 10000 if (preferred_major is not None and major == preferred_major) else 0
            scored.append((match_bonus, major, path))
        except Exception:
            continue

    if not scored:
        return ""
    scored.sort(reverse=True)
    return scored[0][2]


def _existing_env_path(*names: str) -> str:
    for name in names:
        value = str(os.environ.get(name) or "").strip()
        if not value:
            continue
        expanded = os.path.expanduser(value)
        if os.path.exists(expanded):
            return expanded
        resolved = shutil.which(value)
        if resolved:
            return resolved
    return ""


CHROME_BINARY_EXECUTABLE_PATH = (
    _existing_env_path("CHROME_BINARY_EXECUTABLE_PATH")
    or _bundled_chrome_for_testing_binary()
)
CHROMEDRIVER_EXECUTABLE_PATH = (
    _existing_env_path("CHROMEDRIVER_EXECUTABLE_PATH", "RE_ANALYZER_CHROMEDRIVER_PATH")
    or _bundled_chromedriver_binary()
)
# Use chrome://version/ to locate the user_data_dir path.
CHROME_USER_DATA_DIR = os.environ.get('CHROME_USER_DATA_DIR')
CHROME_PROFILE_DIRECTORY = os.environ.get("CHROME_PROFILE_DIRECTORY")
CHROMEDRIVER_STARTUP_LOCK_MODE = os.environ.get("CHROMEDRIVER_STARTUP_LOCK_MODE", "auto")
CHROMEDRIVER_USER_MULTI_PROCS = os.environ.get("CHROMEDRIVER_USER_MULTI_PROCS", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ALLOW_INSECURE_CERTS = os.environ.get("RE_ANALYZER_ALLOW_INSECURE_CERTS", "").lower() in {"1", "true", "yes", "on"}
DEFAULT_WINDOW_SIZE = os.environ.get("RE_ANALYZER_CHROME_WINDOW_SIZE", "").strip()
LOG_BROWSER_CONFIG = os.environ.get("RE_ANALYZER_LOG_BROWSER_CONFIG", "").lower() in {"1", "true", "yes", "on"}
UC_VERSION_MAIN_OVERRIDE = os.environ.get("RE_ANALYZER_UC_VERSION_MAIN", "").strip()

# ── macOS: re-sign patched chromedriver with an ad-hoc signature ───────────────
# UC patches the chromedriver binary to remove the cdc_ injection block, which
# invalidates its code signature. macOS SIP kills the process on launch if the
# signature is bad. We hook patch_exe() — the method that writes the modified
# binary — and run `codesign -f -s -` immediately after, before UC tries to
# start the binary. `-s -` is an ad-hoc signature requiring no certificate.
if platform.system() == "Darwin":
    try:
        _orig_patch_exe = uc.Patcher.patch_exe

        def _patch_exe_and_resign(self):
            result = _orig_patch_exe(self)
            try:
                subprocess.run(
                    ["codesign", "-f", "-s", "-", self.executable_path],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except Exception:
                pass
            return result

        uc.Patcher.patch_exe = _patch_exe_and_resign
    except Exception:
        pass


def _has_user_data_dir(path: Optional[str]) -> bool:
    return bool(path) and os.path.exists(path)

def _local_or_bundled_data_file(name: str) -> str:
    local_path = os.path.join(DATA_PATH, name)
    return local_path if os.path.exists(local_path) else os.path.join(DEFAULT_DATA_PATH, name)


MUNICIPALITIES_DATA_PATH = _local_or_bundled_data_file('florida_municipalities_data.txt')
ZIP_CODES_DATA_PATH = _local_or_bundled_data_file('florida_zip_codes.txt')

def _parse_chrome_major_version(output: str) -> Optional[int]:
    text = str(output or "").strip()
    if not text:
        return None
    match = re.search(r"\b(?:Chrome|Chromium)\b[^\d]*(\d{2,4})\.", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"\b(\d{2,4})\.", text)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _detect_chrome_major_version(binary_path: str) -> Optional[int]:
    path = str(binary_path or "").strip()
    if not path:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except Exception:
        return None
    return _parse_chrome_major_version((proc.stdout or proc.stderr or "").strip())


def _fallback_chrome_binary_path() -> Optional[str]:
    system = platform.system()
    if system == "Darwin":
        candidates = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        )
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return None
    if system == "Windows":
        prefixes = []
        for env_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            value = os.environ.get(env_key)
            if value:
                prefixes.append(value)
        suffixes = (
            os.path.join("Google", "Chrome", "Application", "chrome.exe"),
            os.path.join("Chromium", "Application", "chrome.exe"),
        )
        for prefix in prefixes:
            for suffix in suffixes:
                candidate = os.path.join(prefix, suffix)
                if os.path.exists(candidate):
                    return candidate
        return None

    # Linux or anything else.
    for candidate in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _resolve_chrome_binary_path(explicit_path: Optional[str]) -> Optional[str]:
    path = str(explicit_path or "").strip()
    if path:
        expanded = os.path.expanduser(path)
        if os.path.exists(expanded):
            return expanded
        resolved = shutil.which(path)
        if resolved:
            return resolved
    try:
        found = uc.find_chrome_executable()
        if found:
            return found
    except Exception:
        pass
    return _fallback_chrome_binary_path()


class ManagedChromeDriver(uc.Chrome):
    def __init__(self, *args, ignore_detection=False, **kwargs):
        self.ignore_detection = ignore_detection
        super().__init__(*args, **kwargs)
    
    def get(self, url):
        super().get(url)
        if self.ignore_detection:
            return
        # If a verification screen is detected, wait for a human to resolve it.
        # This intentionally does not attempt to bypass challenges automatically.
        try:
            from re_analyzer.scrapers.page_diagnostics import detect_challenge, wait_for_manual_challenge
        except Exception:
            detect_challenge = None
            wait_for_manual_challenge = None

        if detect_challenge and wait_for_manual_challenge:
            try:
                challenge = detect_challenge(self)
            except Exception:
                challenge = {}
            if challenge.get("is_challenge"):
                # Headless mode cannot be resolved via human-in-the-loop (HIL).
                # Do not rely on the UA containing "HeadlessChrome": newer headless
                # modes + undetected-chromedriver often remove that substring.
                is_headless = False
                try:
                    driver_config = getattr(self, "_re_analyzer_driver_config", None)
                    is_headless = bool(getattr(driver_config, "headless", False)) if driver_config else False
                except Exception:
                    is_headless = False
                if is_headless:
                    print(
                        "Verification detected in headless mode; manual resolution is not possible. "
                        f"matched={challenge.get('matched_patterns') or []}",
                        flush=True,
                    )
                    return
                wait_seconds = None
                try:
                    driver_config = getattr(self, "_re_analyzer_driver_config", None)
                    wait_seconds = getattr(driver_config, "manual_challenge_wait_seconds", None) if driver_config else None
                except Exception:
                    wait_seconds = None
                if wait_seconds is None or float(wait_seconds) > 0:
                    wait_for_manual_challenge(self, wait_seconds=wait_seconds, poll_seconds=5.0)
            return

        # Legacy fallback: only checks for the px-captcha wrapper element.
        try:
            is_first_attempt = True
            while self.find_element(By.ID, "px-captcha-wrapper"):
                if is_first_attempt:
                    print("\n")
                print("Verification detected; waiting for manual resolution in the browser.", end="\r")
                time.sleep(10)
                is_first_attempt = False
        except Exception:
            pass


def _maybe_wait_for_challenge(driver, driver_config: "DriverConfig"):
    """
    Apply the same "human-in-the-loop" challenge pause behavior used by
    ManagedChromeDriver.get(), but for plain Selenium drivers.

    This never attempts automated challenge solving; it only waits when a
    visible block is detected and we're not in headless mode.
    """
    if not driver_config or driver_config.ignore_detection:
        return
    try:
        from re_analyzer.scrapers.page_diagnostics import detect_challenge, wait_for_manual_challenge
    except Exception:
        return
    try:
        challenge = detect_challenge(driver)
    except Exception:
        challenge = {}
    if not challenge.get("is_challenge"):
        return
    if bool(getattr(driver_config, "headless", False)):
        print(
            "Verification detected in headless mode; manual resolution is not possible. "
            f"matched={challenge.get('matched_patterns') or []}",
            flush=True,
        )
        return
    try:
        wait_seconds = getattr(driver_config, "manual_challenge_wait_seconds", 45.0)
        if wait_seconds is None or float(wait_seconds) > 0:
            wait_for_manual_challenge(driver, wait_seconds=wait_seconds, poll_seconds=5.0)
    except Exception:
        return

# Compatibility alias for older modules that referenced ZillowChromeDriver directly.
ZillowChromeDriver = ManagedChromeDriver

@dataclass
class DriverConfig:
    browser_executable_path: Optional[str] = CHROME_BINARY_EXECUTABLE_PATH
    chromedriver_executable_path: Optional[str] = CHROMEDRIVER_EXECUTABLE_PATH
    user_data_dir: Optional[str] = CHROME_USER_DATA_DIR
    profile_directory: Optional[str] = CHROME_PROFILE_DIRECTORY
    ignore_detection: bool = False
    headless: bool = False
    random_profile: bool = False
    clean_profile: bool = False
    window_rect: Optional[Dict[str, int]] = None
    enforce_window_rect: bool = False
    allow_insecure_certs: bool = ALLOW_INSECURE_CERTS
    user_multi_procs: bool = CHROMEDRIVER_USER_MULTI_PROCS
    startup_lock_mode: str = CHROMEDRIVER_STARTUP_LOCK_MODE
    # Human-in-the-loop (HIL) wait time when an interactive verification page is detected.
    # - None: wait indefinitely
    # - 0: do not wait (capture diagnostics and continue)
    manual_challenge_wait_seconds: Optional[float] = 45.0

    @classmethod
    def from_overrides(
        cls,
        headless: bool = False,
        ignore_detection: bool = False,
        random_profile: bool = False,
        clean_profile: bool = False,
        window_rect: Optional[Dict[str, int]] = None,
        enforce_window_rect: bool = False,
        browser_executable_path: Optional[str] = None,
        chromedriver_executable_path: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        profile_directory: Optional[str] = None,
        allow_insecure_certs: Optional[bool] = None,
        user_multi_procs: Optional[bool] = None,
        startup_lock_mode: Optional[str] = None,
        manual_challenge_wait_seconds: Optional[float] = None,
    ) -> "DriverConfig":
        if manual_challenge_wait_seconds is None:
            raw = str(os.environ.get("RE_ANALYZER_MANUAL_CHALLENGE_WAIT_SECONDS", "45") or "").strip().lower()
            if raw in {"none", "inf", "infinite"}:
                manual_challenge_wait_seconds = None
            else:
                try:
                    manual_challenge_wait_seconds = float(raw)
                except Exception:
                    manual_challenge_wait_seconds = 45.0
        return cls(
            browser_executable_path=browser_executable_path or CHROME_BINARY_EXECUTABLE_PATH,
            chromedriver_executable_path=chromedriver_executable_path or CHROMEDRIVER_EXECUTABLE_PATH,
            user_data_dir=user_data_dir or CHROME_USER_DATA_DIR,
            profile_directory=profile_directory or CHROME_PROFILE_DIRECTORY,
            ignore_detection=ignore_detection,
            headless=headless,
            random_profile=random_profile,
            clean_profile=clean_profile,
            window_rect=window_rect,
            enforce_window_rect=enforce_window_rect,
            allow_insecure_certs=ALLOW_INSECURE_CERTS if allow_insecure_certs is None else allow_insecure_certs,
            user_multi_procs=CHROMEDRIVER_USER_MULTI_PROCS if user_multi_procs is None else user_multi_procs,
            startup_lock_mode=startup_lock_mode or CHROMEDRIVER_STARTUP_LOCK_MODE,
            manual_challenge_wait_seconds=manual_challenge_wait_seconds,
        )

class ChromeProfileManager():
    min_profile_number = PROJECT_CONFIG['min_profile_number']
    max_profile_number = PROJECT_CONFIG['max_profile_number']
    def __init__(self):
        # Legacy profile rotation remains available for older local workflows.
        self.current_profile_number = self.next_profile_number(random_profile=True)

    def next_profile_number(self, random_profile=False):
        if random_profile:
            self.current_profile_number = rd.randint(self.min_profile_number, self.max_profile_number)
        else:
            self.current_profile_number = self.min_profile_number + (self.current_profile_number - self.min_profile_number + 1) % (self.max_profile_number - self.min_profile_number + 1)
        return self.current_profile_number

PROFILE_CACHE_FILES = ['Cookies', 'Cookies-journal', 'History', 'History-journal', 'Visited Links', 'Web Data', 'Web Data-journal', 'Local Storage', 'Session Storage', 'Sessions', 'IndexedDB', 'GPUCache']
chromeProfileManager = ChromeProfileManager()


def clean_profile_data(profile_number=None, profile_directory=None, user_data_dir=None):
    if user_data_dir is None:
        user_data_dir = CHROME_USER_DATA_DIR
    if profile_directory:
        profile_path = os.path.join(user_data_dir, profile_directory)
    else:
        profile_path = os.path.join(user_data_dir, f"Profile {profile_number}")
    for cache_file in PROFILE_CACHE_FILES:
        profile_cache_path = os.path.join(profile_path, cache_file)
        if os.path.exists(profile_cache_path):
            if os.path.isfile(profile_cache_path):
                os.remove(profile_cache_path)
            elif os.path.isdir(profile_cache_path):
                shutil.rmtree(profile_cache_path)

def kill_chrome_leaks(kill_unix_leaks=False):
    if platform.system() == "Windows":
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "chrome_for_testing.exe"],
                check=True,
                stdout=subprocess.DEVNULL,  # Suppress standard output
                stderr=subprocess.DEVNULL   # Suppress errors
            )
        except subprocess.CalledProcessError as e:
            pass
    elif kill_unix_leaks:
        try:
            subprocess.run(
                ["pkill", "-f", "chrome_for_testing"],
                check=True,
                stdout=subprocess.DEVNULL,  # Suppress standard output
                stderr=subprocess.DEVNULL   # Suppress errors
            )
        except subprocess.CalledProcessError as e:
            pass

def _parse_window_size(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return 1280, 900
    match = re.match(r"^(?P<w>\\d{3,5})\\s*[x, ]\\s*(?P<h>\\d{3,5})$", raw)
    if not match:
        return 1280, 900
    try:
        width = int(match.group("w"))
        height = int(match.group("h"))
    except (TypeError, ValueError):
        return 1280, 900
    if width <= 0 or height <= 0:
        return 1280, 900
    return width, height

def _looks_like_primary_chrome_user_data_dir(path):
    """
    Best-effort heuristic to detect when CHROME_USER_DATA_DIR points at a real
    interactive Chrome profile directory (vs a dedicated automation directory).

    When we are pointed at a primary user-data-dir and no explicit
    CHROME_PROFILE_DIRECTORY is provided, we prefer using the existing profile
    rotation logic to reduce the risk of accidentally using the user's Default
    profile.
    """
    text = str(path or "").replace("\\", "/").lower()
    if "chrome for testing" in text:
        return False
    return any(token in text for token in ("/google/chrome", "/google chrome", "/chrome/user data"))

def get_chrome_options(driver_config: DriverConfig) -> uc.ChromeOptions:
    options = uc.ChromeOptions()
    if driver_config.headless:
        options.add_argument("--headless=new")

    if _has_user_data_dir(driver_config.user_data_dir):
        profile_directory = str(driver_config.profile_directory or "").strip()
        if profile_directory:
            PROJECT_CONFIG['profile_number'] = None
        else:
            use_rotation = _looks_like_primary_chrome_user_data_dir(driver_config.user_data_dir)
            if use_rotation:
                profile_number = chromeProfileManager.next_profile_number(random_profile=driver_config.random_profile)
                PROJECT_CONFIG['profile_number'] = profile_number
                profile_directory = f"Profile {profile_number}"
            else:
                PROJECT_CONFIG['profile_number'] = None
                profile_directory = "Default"
        PROJECT_CONFIG['profile_directory'] = profile_directory
        options.add_argument(f"--profile-directory={profile_directory}")

    if driver_config.allow_insecure_certs:
        options.add_argument("--ignore-ssl-errors=yes")
        options.add_argument("--ignore-certificate-errors")

    if driver_config.window_rect:
        options.add_argument(f"--window-position={int(driver_config.window_rect['x'])},{int(driver_config.window_rect['y'])}")
        options.add_argument(f"--window-size={int(driver_config.window_rect['width'])},{int(driver_config.window_rect['height'])}")
    elif DEFAULT_WINDOW_SIZE:
        width, height = _parse_window_size(DEFAULT_WINDOW_SIZE)
        options.add_argument(f"--window-size={int(width)},{int(height)}")

    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    # Randomize the DevTools endpoint port to break CDP side-channel fingerprinting
    # used by Pixelscan and similar checkers that probe for a predictable debugging port.
    options.add_argument("--remote-debugging-port=0")
    # Enable browser-level log capture so driver.get_log('browser') works.
    # This lets _extract_perimeter_x_cookies collect console errors and JS exceptions.
    try:
        options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    except Exception:
        pass
    return options

@contextmanager
def chromedriver_startup_lock(mode=None):
    """
    undetected_chromedriver patches a shared driver binary during startup. When
    multiple provider processes launch at the same instant, that patch step can
    race. This lock only serializes driver construction; scraping still runs in
    parallel once the browsers are open.
    """
    mode = (mode or CHROMEDRIVER_STARTUP_LOCK_MODE or "auto").lower()
    # Note: Even when `user_multi_procs` is enabled, UC can still patch a shared
    # chromedriver binary on disk (especially when we pin a repo-bundled driver).
    # Launching multiple provider processes concurrently without a cross-process
    # lock can corrupt the driver or trip macOS code-signing checks.
    if mode == "never":
        yield
        return
    lock_path = os.path.join(tempfile.gettempdir(), "real_estate_rover_uc_driver_startup.lock")
    lock_file = open(lock_path, "w")
    try:
        if mode != "never" and platform.system() != "Windows":
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield
    finally:
        if platform.system() != "Windows":
            try:
                import fcntl
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except Exception:
                pass
        lock_file.close()

@contextmanager
def get_selenium_driver(
    url,
    driver_config: Optional[DriverConfig] = None,
    headless=False,
    ignore_detection=False,
    random_profile=False,
    clean_profile=False,
    window_rect=None,
    enforce_window_rect=False,
):
    driver = None
    if driver_config is None:
        driver_config = DriverConfig.from_overrides(
            headless=headless,
            ignore_detection=ignore_detection,
            random_profile=random_profile,
            clean_profile=clean_profile,
            window_rect=window_rect,
            enforce_window_rect=enforce_window_rect,
        )

    try:
        resolved_binary = _resolve_chrome_binary_path(driver_config.browser_executable_path)
        if resolved_binary and resolved_binary != driver_config.browser_executable_path:
            driver_config.browser_executable_path = resolved_binary

        version_main = None
        if UC_VERSION_MAIN_OVERRIDE and UC_VERSION_MAIN_OVERRIDE.isdigit():
            try:
                version_main = int(UC_VERSION_MAIN_OVERRIDE)
            except (TypeError, ValueError):
                version_main = None
        if version_main is None and resolved_binary:
            version_main = _detect_chrome_major_version(resolved_binary)

        options = get_chrome_options(driver_config)
        if driver_config.clean_profile:
            clean_profile_data(
                PROJECT_CONFIG['profile_number'],
                profile_directory=PROJECT_CONFIG['profile_directory'],
                user_data_dir=driver_config.user_data_dir,
            )
        with chromedriver_startup_lock(mode=driver_config.startup_lock_mode):
            driver_kwargs = {
                "options": options,
                "browser_executable_path": driver_config.browser_executable_path,
                "ignore_detection": driver_config.ignore_detection,
                "user_multi_procs": driver_config.user_multi_procs,
            }
            chromedriver_path = str(driver_config.chromedriver_executable_path or "").strip()
            if chromedriver_path:
                expanded_chromedriver_path = os.path.expanduser(chromedriver_path)
                if os.path.exists(expanded_chromedriver_path):
                    chromedriver_path = expanded_chromedriver_path
                else:
                    print(f"[browser] configured chromedriver path does not exist: {chromedriver_path}", flush=True)
                    chromedriver_path = ""

            # If we have a preferred Chrome major, try to keep chromedriver aligned.
            # This avoids UC falling back to a stale cached driver (often v123) when a newer
            # repo-pinned driver exists under ChromeAssets.
            if version_main:
                desired = int(version_main)
                effective_major = _detect_chromedriver_major_version(chromedriver_path) if chromedriver_path else None
                if effective_major is None or effective_major <= 0:
                    fallback = _pick_bundled_chromedriver_binary(preferred_major=desired)
                    if fallback:
                        chromedriver_path = fallback
                        effective_major = _detect_chromedriver_major_version(chromedriver_path)
                elif effective_major != desired:
                    fallback = _pick_bundled_chromedriver_binary(preferred_major=desired)
                    if fallback and fallback != chromedriver_path:
                        print(
                            f"[browser] chromedriver major mismatch (have={effective_major}, want={desired}); "
                            f"switching to bundled driver: {fallback}",
                            flush=True,
                        )
                        chromedriver_path = fallback
                        effective_major = _detect_chromedriver_major_version(chromedriver_path)
                if not chromedriver_path:
                    print(
                        f"[browser] no chromedriver path resolved for Chrome major {desired}; "
                        "undetected-chromedriver may fall back to a cached driver. "
                        "Set RE_ANALYZER_CHROMEDRIVER_PATH to a matching binary to avoid version mismatches.",
                        flush=True,
                    )

            if chromedriver_path:
                driver_kwargs["driver_executable_path"] = chromedriver_path
                driver_config.chromedriver_executable_path = chromedriver_path
                driver_kwargs["user_multi_procs"] = False
                driver_config.user_multi_procs = False
            if _has_user_data_dir(driver_config.user_data_dir):
                driver_kwargs["user_data_dir"] = driver_config.user_data_dir
            if version_main:
                driver_kwargs["version_main"] = version_main
            use_plain_selenium = False
            if use_plain_selenium:
                # On modern macOS, undetected-chromedriver's patch step can invalidate
                # chromedriver's code signature and cause an immediate SIGKILL.
                # Prefer a plain Selenium session by default.
                chrome_service = ChromeService(executable_path=driver_kwargs.get("driver_executable_path") or "")
                if driver_config.browser_executable_path:
                    try:
                        options.binary_location = driver_config.browser_executable_path
                    except Exception:
                        pass
                if _has_user_data_dir(driver_config.user_data_dir):
                    try:
                        options.add_argument(f"--user-data-dir={driver_config.user_data_dir}")
                    except Exception:
                        pass
                # If no chromedriver path is configured, Selenium Manager will resolve one.
                if chrome_service.path:
                    driver = webdriver.Chrome(service=chrome_service, options=options)
                else:
                    driver = webdriver.Chrome(options=options)
            else:
                driver = ManagedChromeDriver(
                    **driver_kwargs,
                )
        try:
            driver._re_analyzer_driver_config = driver_config
            driver._re_analyzer_options = options
        except Exception:
            pass
        if LOG_BROWSER_CONFIG:
            try:
                print(
                    "[browser] started "
                    f"headless={bool(driver_config.headless)} "
                    f"binary={driver_config.browser_executable_path or '<auto>'} "
                    f"user_data_dir={driver_config.user_data_dir or '<temp>'} "
                    f"profile={PROJECT_CONFIG['profile_directory'] or ''} "
                    f"insecure_certs={bool(driver_config.allow_insecure_certs)}",
                    flush=True,
                )
            except Exception:
                pass
            try:
                from re_analyzer.scrapers.browser_diagnostics import environment_versions, summarize_capabilities

                versions = environment_versions()
                caps_summary = summarize_capabilities(getattr(driver, "capabilities", {}) or {})
                chromedriver_version = ""
                try:
                    chromedriver_version = (caps_summary.get("chromedriver") or {}).get("version") or ""
                except Exception:
                    chromedriver_version = ""
                print(
                    "[browser] versions "
                    f"selenium={versions.get('selenium') or '?'} "
                    f"uc={versions.get('undetected_chromedriver') or '?'} "
                    f"chrome={caps_summary.get('browser_version') or '?'} "
                    f"chromedriver={chromedriver_version or '?'}",
                    flush=True,
                )
            except Exception:
                pass
        if driver_config.window_rect and driver_config.enforce_window_rect:
            driver.set_window_rect(
                int(driver_config.window_rect["x"]),
                int(driver_config.window_rect["y"]),
                int(driver_config.window_rect["width"]),
                int(driver_config.window_rect["height"]),
            )
        # Guard against infinite loads (some challenge/interstitial pages never fire "load").
        try:
            # Default is intentionally generous to avoid breaking scraper flows, while still
            # preventing true infinite loads (some challenge pages never fire "load").
            driver.set_page_load_timeout(int(os.environ.get("RE_ANALYZER_PAGE_LOAD_TIMEOUT_SECONDS", "120")))
        except Exception:
            pass
        try:
            # Chrome for Testing only reports "Chromium" brand in Sec-Ch-Ua / navigator.userAgentData.
            # Real Chrome also includes "Google Chrome". PerimeterX/HUMAN check this header at the
            # network level — mismatched brand list is a direct CfT fingerprint.
            # Emulation.setUserAgentOverride with userAgentMetadata patches both the HTTP header
            # and the JS navigator.userAgentData API in a single CDP call.
            _bv = str((getattr(driver, "capabilities", None) or {}).get("browserVersion") or "")
            _mv = str(version_main or _parse_chrome_major_version(_bv) or 149)
            _fv = _bv if re.search(r"\d+\.\d+\.\d+\.\d+", _bv) else f"{_mv}.0.0.0"
            _ua = ""
            try:
                _ua = str(driver.execute_script("return navigator.userAgent;") or "")
            except Exception:
                pass
            if not _ua:
                _ua = (
                    f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    f"AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{_mv}.0.0.0 Safari/537.36"
                )
            _arch = "arm" if platform.machine().lower() in ("arm64", "aarch64") else "x86"
            _plat = "macOS" if platform.system() == "Darwin" else platform.system()
            _plat_ver = (platform.mac_ver()[0] or "") if platform.system() == "Darwin" else ""
            driver.execute_cdp_cmd(
                "Emulation.setUserAgentOverride",
                {
                    "userAgent": _ua,
                    "userAgentMetadata": {
                        "brands": [
                            {"brand": "Not)A;Brand", "version": "24"},
                            {"brand": "Chromium", "version": _mv},
                            {"brand": "Google Chrome", "version": _mv},
                        ],
                        "fullVersionList": [
                            {"brand": "Not)A;Brand", "version": "24.0.0.0"},
                            {"brand": "Chromium", "version": _fv},
                            {"brand": "Google Chrome", "version": _fv},
                        ],
                        "platform": _plat,
                        "platformVersion": _plat_ver,
                        "architecture": _arch,
                        "model": "",
                        "mobile": False,
                    },
                },
            )
        except Exception:
            pass
        try:
            # Disable Chrome's C++ NavigatorAutomation at the CDP level.
            # JS layers alone cannot fix navigator.webdriver on real HTTPS pages: after
            # addScriptToEvaluateOnNewDocument fires, Chrome's C++ Blink runtime re-activates
            # WebDriver mode and overwrites any JS property lock.
            # Emulation.setAutomationOverride(enabled=False) flips the C++ automation flag
            # before any navigation, making navigator.webdriver return undefined natively.
            driver.execute_cdp_cmd("Emulation.setAutomationOverride", {"enabled": False})
        except Exception:
            pass
        try:
            # Comprehensive stealth injection at document_start.
            #
            # Plain Selenium on macOS does not apply UC's binary-level patches, so we
            # use Page.addScriptToEvaluateOnNewDocument to clean up the main leaks.
            #
            # Layered strategy (each layer is an independent fallback):
            #
            #  1 & 2. webdriver on Navigator prototype + instance
            #         Defense-in-depth alongside Emulation.setAutomationOverride above.
            #         configurable:false blocks any subsequent C++ re-write attempt.
            #
            #  3. window.navigator Proxy fallback
            #         When Blink defines navigator.webdriver as non-configurable at the C++
            #         level before any JS runs, Object.defineProperty throws and layers 1/2
            #         silently fail.  If window.navigator is itself configurable we replace
            #         it with a cached Proxy that returns undefined for 'webdriver'.
            #
            #  4. Object.getOwnPropertyDescriptor interception
            #         fpscanner and fpCollect call GOPD directly to inspect the descriptor.
            #         Without this they see our getter and flag WEBDRIVER even if the value
            #         reads as undefined.  We return undefined for the webdriver key so the
            #         property appears absent to descriptor-based checks.
            #
            #  5. Function.prototype.toString interception
            #         GOPD is now a JS function, not native code. Fingerprinters call
            #         someFunc.toString() to check for '[native code]'. We return the
            #         original native string for functions we patched.
            #
            #  6. cdc_ properties left on window by ChromeDriver
            #         Pixelscan and HUMAN/PerimeterX scan for window.cdc_* symbols as a
            #         direct ChromeDriver fingerprint. Deleted here at document_start.
            #
            #  7. documentElement webdriver attribute
            #         Older Blink versions set document.documentElement['webdriver']='true'
            #         as an HTML attribute. Removed on document_start and DOMContentLoaded.
            #
            #  8. Blob constructor patching for blob-sourced workers
            #         WorkerNavigator is a separate class — prototype patches above only
            #         cover the main window. Prepend a self-contained fix to every
            #         application/javascript Blob so DedicatedWorkers created from Blob
            #         URLs have navigator.webdriver === undefined.
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """(function () {
  // ── Capture originals before any patching ─────────────────────────────────
  var _origGOPD      = Object.getOwnPropertyDescriptor;
  var _origDefProp   = Object.defineProperty;
  var _origFnToStr   = Function.prototype.toString;

  // ── 1: navigator.webdriver — native-state check ──────────────────────────
  // We intentionally do NOT redefine the prototype getter here.
  //
  // Replacing the native C++ getter with a JS function makes it detectable:
  //   - fpCollect iterates Object.getOwnPropertyNames(proto) and calls
  //     Function.prototype.toString on each getter. A JS getter shows custom
  //     source instead of "[native code]", so fpCollect logs webDriver: true.
  //   - fpscanner.analyseFingerprint() reads the same fpCollect output and
  //     marks WEBDRIVER: FAIL.
  //
  // The correct approach: rely on Emulation.setAutomationOverride(enabled=False)
  // which makes Chrome's native C++ getter return false. The getter stays native,
  // toString shows "[native code]", and descriptor inspection passes cleanly.
  // Layer 3 (Proxy) is always installed to intercept the `in` operator via the
  // `has` trap — fpCollect uses `'webdriver' in navigator`, which traverses the
  // prototype chain and would return true without the Proxy trap.
  var _protoOk = false;
  try {
    var _protoNav = Object.getPrototypeOf(navigator);
    var _wdProtoDesc = _origGOPD(_protoNav, 'webdriver');
    if (!_wdProtoDesc) {
      _protoOk = true; // property absent from prototype — no leak
    } else {
      var _wdNativeVal = _wdProtoDesc.get ? _wdProtoDesc.get.call(navigator) : _wdProtoDesc.value;
      _protoOk = !_wdNativeVal; // safe when falsy (false or undefined)
    }
  } catch (e) {}

  // ── 3: window.navigator Proxy — always installed ─────────────────────────
  // The Proxy is installed unconditionally (not just as a fallback) because
  // fpCollect checks 'webdriver' in navigator (the `in` operator), not just
  // navigator.webdriver. The `in` operator traverses the prototype chain, so
  // the native webdriver property on Navigator.prototype makes it always true.
  // The Proxy's `has` trap intercepts `'webdriver' in navigator` → false.
  //
  // Additionally, hasOwnProperty.call(navigator, 'webdriver') invokes the
  // Proxy's getOwnPropertyDescriptor trap → undefined → false. This covers
  // Sannysoft's _.has(navigator, 'webdriver') path even without Layer 2.
  try {
    var _wnd = _origGOPD(window, 'navigator');
    if (_wnd && _wnd.configurable) {
      var _origNav = window.navigator;
      var _navProxy = null;
      _origDefProp(window, 'navigator', {
        get: function () {
          if (!_navProxy) {
            _navProxy = new Proxy(_origNav, {
              get: function (t, p) {
                if (p === 'webdriver') return false;
                var v = t[p];
                return typeof v === 'function' ? v.bind(t) : v;
              },
              has: function (t, p) { return p === 'webdriver' ? false : (p in t); },
              getOwnPropertyDescriptor: function (t, p) {
                return p === 'webdriver' ? undefined : _origGOPD(t, p);
              }
            });
          }
          return _navProxy;
        },
        configurable: false,
        enumerable: true
      });
    }
  } catch (e) {}

  // ── 4: Object.getOwnPropertyDescriptor interception ───────────────────────
  // When the Proxy is active (_protoOk=false) and code calls
  // Object.getOwnPropertyDescriptor on the prototype (bypassing the Proxy),
  // return undefined so descriptor-based probes don't see the native "true" getter.
  // When the native getter is already safe (_protoOk=true) we leave the prototype
  // descriptor untouched — fpCollect reads it and correctly shows [native code].
  var _gopdNative = '';
  try { _gopdNative = _origFnToStr.call(_origGOPD); } catch (e) {}
  try {
    Object.getOwnPropertyDescriptor = function (obj, prop) {
      if (prop === 'webdriver') {
        try {
          if (obj === navigator) return undefined;
          if (!_protoOk && obj === Object.getPrototypeOf(navigator)) return undefined;
        } catch (_e) {}
      }
      return _origGOPD.apply(this, arguments);
    };
  } catch (e) {}

  // ── 5: Function.prototype.toString — make patched functions look native ────
  try {
    var _fnToStrNative = _origFnToStr.call(_origFnToStr);
    var _nativeStrs = new Map([[Object.getOwnPropertyDescriptor, _gopdNative]]);
    Function.prototype.toString = function () {
      if (_nativeStrs.has(this)) return _nativeStrs.get(this);
      return _origFnToStr.call(this);
    };
    _nativeStrs.set(Function.prototype.toString, _fnToStrNative);
  } catch (e) {}

  // ── 6: Remove cdc_ properties (ChromeDriver fingerprint artifacts) ─────────
  try {
    var _names = Object.getOwnPropertyNames(window);
    for (var _i = 0; _i < _names.length; _i++) {
      var _k = _names[_i];
      if (_k.length > 4 && _k[0] === 'c' && _k[1] === 'd' && _k[2] === 'c' && _k[3] === '_') {
        try { delete window[_k]; } catch (_e) {
          try {
            _origDefProp(window, _k, { get: function () { return undefined; }, configurable: true });
          } catch (_e2) {}
        }
      }
    }
  } catch (e) {}

  // ── 7: documentElement webdriver attribute cleanup ─────────────────────────
  try {
    var _rmWdAttr = function () {
      try {
        if (document.documentElement && document.documentElement.hasAttribute('webdriver')) {
          document.documentElement.removeAttribute('webdriver');
        }
      } catch (_e) {}
    };
    _rmWdAttr();
    document.addEventListener('DOMContentLoaded', _rmWdAttr, { once: true, capture: true });
  } catch (e) {}

  // ── 8: PointerEvent pressure ─────────────────────────────────────────────
  // CDP Input.dispatchMouseEvent sends pressure=0.5 on press, but Chrome does not
  // forward that value into the synthesised PointerEvent.pressure property — the
  // getter always returns 0 regardless of the CDP field. Real desktop mice return
  // 0.5 while any button is held (PointerEvent spec §5.2). Override the prototype
  // getter to restore that semantics so bot-detection sensors see realistic values.
  try {
    var _peProto = typeof PointerEvent !== 'undefined' && PointerEvent.prototype;
    var _peDesc = _peProto && Object.getOwnPropertyDescriptor(_peProto, 'pressure');
    if (_peDesc && typeof _peDesc.get === 'function') {
      Object.defineProperty(_peProto, 'pressure', {
        get: function () { return this.buttons > 0 ? 0.5 : 0; },
        configurable: true,
      });
    }
  } catch (e) {}

  // ── 9: Blob patch for blob-sourced workers ─────────────────────────────────
  // Prepend a self-contained webdriver fix to every application/javascript Blob
  // so DedicatedWorkers created from Blob URLs also have navigator.webdriver === undefined
  // (matching real, non-automated Chrome) rather than false.
  //
  // Strategy: redefine the prototype getter to return undefined, then wrap
  // globalThis.navigator in a Proxy whose `has` trap hides the property from
  // the `in` operator — the same approach used in the main frame.
  // configurable:true is required so the Proxy approach (which re-defines the
  // prototype) can co-exist without throwing on a second defineProperty call.
  try {
    var _NB = window.Blob;
    var _wp = (
      '(function(){' +
      'try{' +
      // Step A: make the prototype getter return undefined
      'var _p=Object.getPrototypeOf(navigator);' +
      'if(_p){Object.defineProperty(_p,"webdriver",{get:function(){return undefined;},configurable:true,enumerable:false});}' +
      // Step B: Proxy over globalThis.navigator to intercept `in` and get
      'var _n=globalThis.navigator||self.navigator;' +
      'if(_n){' +
      'var _px=new Proxy(_n,{' +
      'get:function(t,k){return k==="webdriver"?undefined:t[k];},' +
      'has:function(t,k){return k==="webdriver"?false:(k in t);},' +
      'getOwnPropertyDescriptor:function(t,k){return k==="webdriver"?undefined:Object.getOwnPropertyDescriptor(t,k);}' +
      '});' +
      'try{Object.defineProperty(self,"navigator",{get:function(){return _px;},configurable:true,enumerable:true});}catch(e){}' +
      '}' +
      '}catch(e){}' +
      '})();'
    );
    var _PB = function (parts, opts) {
      var t = String((opts && opts.type) || '').toLowerCase();
      if (t === 'application/javascript' || t === 'text/javascript') {
        return new _NB([_wp].concat(Array.prototype.slice.call(parts || [])), opts);
      }
      return new _NB(parts, opts);
    };
    _PB.prototype = _NB.prototype;
    _origDefProp(window, 'Blob', { value: _PB, writable: true, configurable: true });
  } catch (e) {}
})();"""
                },
            )
        except Exception:
            pass
        try:
            driver.get(url)
        except TimeoutException as exc:
            print(f"[browser] page load timeout for {url}: {exc}", flush=True)
        try:
            _maybe_wait_for_challenge(driver, driver_config)
        except Exception:
            pass
        try:
            # Settle window: satisfy WAF behavioral telemetry that expects organic
            # human pacing after initial page load before any scraping actions begin.
            # Progressive variable scroll simulates natural reading/exploration.
            _settle_s = rd.uniform(2.0, 4.0)
            time.sleep(_settle_s)
            driver.execute_script(
                """
                (function () {
                    var _steps = Math.floor(3 + Math.random() * 4);
                    var _i = 0;
                    function _tick() {
                        if (_i >= _steps) return;
                        var _dy = Math.floor(80 + Math.random() * 220);
                        window.scrollBy({ top: _dy, behavior: 'smooth' });
                        _i++;
                        setTimeout(_tick, Math.floor(400 + Math.random() * 800));
                    }
                    _tick();
                })();
                """
            )
        except Exception:
            pass
        yield driver
    except Exception as e:
        print(f"An error occurred in the driver context: {e}")
        raise
    finally:
        if driver:
            driver.quit()

def retry_request(project_config):
    max_attempts = project_config['max_reconnect_retries']
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempts in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempts >= max_attempts:
                        break
                    random_delay(5, 10)
            random_delay(15, 30)
            return None
        return wrapper
    return decorator


@retry_request(PROJECT_CONFIG)
def extract_cookies_from_driver(driver, delay):
    random_delay(delay, 2*delay)
    cookies = driver.get_cookies()
    session_cookies = {cookie['name']: cookie['value'] for cookie in cookies}
    return "; ".join([f"{name}={value}" for name, value in session_cookies.items()])


def is_element_in_viewport(driver, element):
    script = """
    var elem = arguments[0], box = elem.getBoundingClientRect(), cx = box.left + box.width / 2, cy = box.top + box.height / 2, e = document.elementFromPoint(cx, cy);
    for (; e; e = e.parentElement) {
        if (e === elem) return true;
    }
    return false;
    """
    return driver.execute_script(script, element)


def scroll_to_element(driver, container_selector, element_selector, max_attempts=10):
    """Scroll to an element until it is visible on the screen."""
    try:
        element = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, element_selector)))
    except Exception as e:
        print(f"Either the container or element could not be located.")
        return
    attempts = 0
    while attempts < max_attempts:
        if is_element_in_viewport(driver, element):
            return # Element is in viewport.
        try:
            driver.execute_script(
                "try { arguments[0].scrollIntoView({block:'center',inline:'center'}); } catch (e) { arguments[0].scrollIntoView(true); }",
                element,
            )
        except Exception:
            pass
        attempts += 1
        random_delay(0.2, 0.6)  # Small delay for layout/paint.

    if attempts >= max_attempts:
        print("Maximum scrolling attempts reached. The element might not be visible.")

def offscreen_click(element, driver):
    driver.execute_script("arguments[0].click();", element)

def move_to_and_click(element, driver, and_hold=False):
    """
    Click an element without synthetic pointer movement.

    Synthetic "move + click/hold" sequences are more likely to be flagged by
    anti-bot systems than plain DOM clicks, and they are not required for our
    current scraping flows.
    """
    if and_hold:
        raise ValueError("move_to_and_click(and_hold=True) is not supported; manual interaction is required.")
    try:
        element.click()
    except Exception:
        offscreen_click(element, driver)

def load_search_metadata():
    zip_code_to_zpids = defaultdict(set)
    for metadata_path in glob.glob(os.path.join(SEARCH_LISTINGS_METADATA_PATH, "*_metadata.json")):
        zip_code = os.path.basename(metadata_path).split("_metadata.json")[0]
        with open(metadata_path, "r") as file:
            metadata = json.load(file)
            zip_code_to_zpids[zip_code].update(metadata.get('zpids', []))
    return zip_code_to_zpids

def load_search_zip_codes():
    with open(ZIP_CODES_DATA_PATH, 'r') as file:
        content = file.read()
        zip_codes = re.split(r"[,\s]+", content.strip())
    values = [int(num) for num in zip_codes if str(num).isdigit()]
    # Keep ordering stable and deterministic for state-wide refresh runs.
    return sorted(set(values))


def what_is_my_ip():
    what_is_my_ip_url = "http://httpbin.org/ip"
    with get_selenium_driver(what_is_my_ip_url) as driver:
        time.sleep(1)
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        try:
            pre_tag_content = soup.find('pre').text
            json_data = json.loads(pre_tag_content)
            ip_address_text = json_data["origin"]
            ip_addresses = [ip.strip() for ip in ip_address_text.split(',')]
            return ip_addresses
        except:
            return



def send_sms_alert(message: str) -> bool:
    """
    Send a free SMS via email-to-SMS gateway.

    Credential resolution order (first non-empty wins):
      gmail:    RE_ANALYZER_ALERT_GMAIL  →  GMAIL_MAIL_USERNAME
      password: RE_ANALYZER_ALERT_APP_PASSWORD  →  GMAIL_MAIL_APP_PASSWORD

    RE_ANALYZER_ALERT_SMS_ADDRESS overrides the default recipient gateway address.
    Returns True if the message was sent, False if credentials are missing or sending failed.
    """
    import smtplib
    from email.mime.text import MIMEText

    gmail = (
        os.environ.get("RE_ANALYZER_ALERT_GMAIL", "").strip()
        or os.environ.get("GMAIL_MAIL_USERNAME", "").strip()
    )
    app_password = (
        os.environ.get("RE_ANALYZER_ALERT_APP_PASSWORD", "").strip()
        or os.environ.get("GMAIL_MAIL_APP_PASSWORD", "").strip()
    )
    sms_address = os.environ.get("RE_ANALYZER_ALERT_SMS_ADDRESS", "9543043151@vtext.com").strip()

    if not gmail or not app_password:
        return False

    body = str(message or "")[:160]
    msg = MIMEText(body)
    msg["From"] = gmail
    msg["To"] = sms_address
    msg["Subject"] = ""
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(gmail, app_password)
            server.sendmail(gmail, sms_address, msg.as_string())
        print(f"[sms-alert] sent to {sms_address}: {body[:80]}", flush=True)
        return True
    except Exception as exc:
        print(f"[sms-alert] failed: {exc}", flush=True)
        return False


if __name__ == '__main__':
    print(what_is_my_ip())
    kill_chrome_leaks()
