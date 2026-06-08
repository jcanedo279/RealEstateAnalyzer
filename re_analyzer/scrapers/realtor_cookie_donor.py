"""
Extract safe realtor.com cookies from a personal Chrome profile and inject them
into a running Selenium WebDriver session via CDP.

Copying a carefully filtered subset of analytics/tracking cookies gives a fresh
scraper session the appearance of a returning user — visit counters, A/B group
assignments, and analytics IDs that real browsers accumulate over time.  The
filter intentionally excludes PerimeterX tokens (_pxvid, _px*), KPSDK state
(KP_UIDz), and authentication cookies so the donor profile's trust score is
never shared or invalidated.

Usage
-----
    from re_analyzer.scrapers.realtor_cookie_donor import inject_donor_cookies

    # Inside _warm_homepage, after the first organic page loads:
    inject_donor_cookies(driver, profile_dirs=["/path/to/Chrome/Profile 12"])

The function is safe to call even when the donor profile is unavailable — it
logs a warning and returns 0 rather than raising.
"""
from __future__ import annotations

import fnmatch
import os
import random
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Cookie safety filter
# ---------------------------------------------------------------------------

# Patterns for cookies that are safe to copy across browser instances.
# These are analytics / A-B testing / ad-attribution cookies whose values don't
# carry any session-level bot-detection state.
_SAFE_PATTERNS: Tuple[str, ...] = (
    "_ga",
    "_ga_*",
    "_gcl_au",
    "_fbp",
    "_uetsid",
    "_uetvid",
    "ajs_anonymous_id",
    "kampyleUserSessionsCount",
    "kampyleUserSession",
    "kampyleSessionPageCounter",
    "ab.storage.*",
    "panoramaId",
    "panoramaIdType",
    "_parsely_visitor",
    "_cc_id",
    "AMCV_*",
    "AMP_TOKEN",
    "crto_*",
    "cto_bundle",
    "cto_bidid",
    "_rdt_uuid",
    "rdVisits",
    "rdfsL",
    "muidn",
)

# Patterns that must NEVER be copied — they carry PerimeterX/KPSDK identity
# or authentication state that would either break the donor profile's trust
# score or hand live session credentials to the scraper.
_BLOCKED_PATTERNS: Tuple[str, ...] = (
    "_pxvid",
    "_px*",
    "KP_UIDz*",
    "__bot*",
    "__rdc_id",
    "g_state",
    "pxcts",
    "pxscts",
    # Auth / session
    "auth*",
    "session*",
    "token*",
    "jwt*",
    "access_token*",
    "refresh_token*",
    "user_id*",
    "uid*",
    "login*",
)


def _name_is_safe(name: str) -> bool:
    lower = name.lower()
    for pattern in _BLOCKED_PATTERNS:
        if fnmatch.fnmatchcase(lower, pattern.lower()):
            return False
    for pattern in _SAFE_PATTERNS:
        if fnmatch.fnmatchcase(lower, pattern.lower()) or fnmatch.fnmatchcase(name, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# macOS Keychain decryption
# ---------------------------------------------------------------------------

_CHROME_EPOCH_OFFSET_S = 11_644_473_600  # seconds between 1601-01-01 and 1970-01-01
_SALT = b"saltysalt"
_ITERATIONS = 1003
_KEY_LENGTH = 16
_IV = b" " * 16  # AES-128-CBC IV used by Chrome

# Cache the derived AES key keyed by keychain service name so we call the
# `security` CLI at most once per process even across multiple profiles.
_key_cache: Dict[str, Optional[bytes]] = {}


def _get_chrome_safe_storage_key(keychain_service: str = "Chrome Safe Storage") -> Optional[bytes]:
    """Retrieve and derive the AES key from the macOS Keychain.  Returns None on failure."""
    if keychain_service in _key_cache:
        return _key_cache[keychain_service]

    password = _read_keychain_password(keychain_service)
    if password is None:
        _key_cache[keychain_service] = None
        return None

    key = _pbkdf2_key(password)
    _key_cache[keychain_service] = key
    return key


def _read_keychain_password(service: str) -> Optional[bytes]:
    """
    Call `security find-generic-password` to read the Chrome safe-storage
    password.  On macOS this may trigger a Keychain access dialog — the 60-
    second timeout gives the user time to approve it.
    """
    account_map = {
        "Chrome Safe Storage": "Chrome",
        "Chrome for Testing Safe Storage": "Chrome for Testing",
        "Chromium Safe Storage": "Chromium",
    }
    account = account_map.get(service, service.replace(" Safe Storage", ""))

    print(
        f"[cookie-donor] Requesting Keychain access for '{service}' — "
        "approve the dialog if one appears.",
        flush=True,
    )

    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service, "-a", account],
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        print(f"[cookie-donor] Keychain request timed out for '{service}' — skipping.", flush=True)
        return None
    except FileNotFoundError:
        print("[cookie-donor] `security` command not found (not macOS?) — skipping.", flush=True)
        return None

    if result.returncode != 0:
        print(
            f"[cookie-donor] Could not read '{service}' from Keychain "
            f"(rc={result.returncode}) — skipping.",
            flush=True,
        )
        return None

    return result.stdout.strip()


def _pbkdf2_key(password: bytes) -> bytes:
    import hashlib

    return hashlib.pbkdf2_hmac("sha1", password, _SALT, _ITERATIONS, dklen=_KEY_LENGTH)


def _decrypt_cookie_value(encrypted: bytes, key: bytes) -> Optional[str]:
    """Decrypt a Chrome-encrypted cookie value.  Returns None on failure."""
    if not encrypted:
        return None
    # v10/v11 prefix (3 bytes) followed by AES-CBC ciphertext.
    prefix = encrypted[:3]
    if prefix not in (b"v10", b"v11"):
        # Unencrypted (old-format or plaintext stored without prefix).
        try:
            return encrypted.decode("utf-8", errors="replace")
        except Exception:
            return None
    ciphertext = encrypted[3:]
    try:
        from Crypto.Cipher import AES

        cipher = AES.new(key, AES.MODE_CBC, IV=_IV)
        decrypted = cipher.decrypt(ciphertext)
        # Remove PKCS#7 padding.
        pad_len = decrypted[-1]
        if 1 <= pad_len <= 16:
            decrypted = decrypted[:-pad_len]
        return decrypted.decode("utf-8", errors="replace")
    except ImportError:
        # pycryptodome not installed — try cryptography package instead.
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend

            cipher = Cipher(algorithms.AES(key), modes.CBC(_IV), backend=default_backend())
            dec = cipher.decryptor()
            decrypted = dec.update(ciphertext) + dec.finalize()
            pad_len = decrypted[-1]
            if 1 <= pad_len <= 16:
                decrypted = decrypted[:-pad_len]
            return decrypted.decode("utf-8", errors="replace")
        except Exception:
            return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Cookie database reading
# ---------------------------------------------------------------------------

def _chrome_ts_to_unix(chrome_us: int) -> float:
    """Convert Chrome microseconds-since-1601 to Unix timestamp."""
    return chrome_us / 1_000_000 - _CHROME_EPOCH_OFFSET_S


def read_realtor_cookies(
    profile_path: str,
    *,
    profile_subdir: str = "Default",
    keychain_service: str = "Chrome Safe Storage",
) -> List[Dict]:
    """
    Read and decrypt realtor.com cookies from a Chrome profile directory.

    Returns a list of cookie dicts suitable for CDP injection, containing only
    cookies that pass the safety filter.  Returns [] on any error.
    """
    cookies_db = Path(profile_path).expanduser().resolve() / profile_subdir / "Network" / "Cookies"
    if not cookies_db.exists():
        # Older Chrome layout: Cookies sits directly in the profile subdir.
        cookies_db = Path(profile_path).expanduser().resolve() / profile_subdir / "Cookies"
    if not cookies_db.exists():
        return []

    aes_key = _get_chrome_safe_storage_key(keychain_service)

    results = []
    try:
        conn = sqlite3.connect(f"file:{cookies_db}?mode=ro&immutable=1", uri=True)
        try:
            rows = conn.execute(
                "SELECT name, encrypted_value, host_key, path, expires_utc, is_secure, is_httponly "
                "FROM cookies WHERE host_key LIKE '%realtor.com'"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[cookie-donor] Could not read {cookies_db}: {exc}", flush=True)
        return []

    now = time.time()
    for name, encrypted_value, host_key, path, expires_utc, is_secure, is_httponly in rows:
        if not _name_is_safe(name):
            continue

        # Decode value.
        if aes_key is not None:
            value = _decrypt_cookie_value(encrypted_value, aes_key)
        else:
            # Keychain unavailable — try to use the raw value if it happens to
            # be stored in plaintext (older Chrome versions, some cookies).
            try:
                raw = bytes(encrypted_value) if isinstance(encrypted_value, memoryview) else encrypted_value
                if raw[:3] not in (b"v10", b"v11"):
                    value = raw.decode("utf-8", errors="replace")
                else:
                    continue  # Encrypted but no key — skip.
            except Exception:
                continue

        if value is None:
            continue

        # Convert Chrome expiry timestamp to Unix seconds.
        if expires_utc:
            expiry_unix = _chrome_ts_to_unix(expires_utc)
            if expiry_unix < now:
                continue  # Already expired.
        else:
            expiry_unix = now + 86_400 * 30  # Default: 30 days.

        # Normalise host_key: Chrome stores leading dots (.realtor.com).
        domain = host_key if host_key.startswith(".") else f".{host_key}"

        results.append({
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "expires": int(expiry_unix),
            "httpOnly": bool(is_httponly),
            "secure": bool(is_secure),
            "sameSite": "None",
        })

    return results


# ---------------------------------------------------------------------------
# Synthetic supplement
# ---------------------------------------------------------------------------

def _synthetic_cookies(existing_names: set) -> List[Dict]:
    """
    Generate minimal synthetic cookies to fill any gaps.

    A fresh _ga gives the session a plausible Google Analytics ID.
    """
    cookies = []
    now = time.time()
    far_future = int(now + 86_400 * 365 * 2)

    if "_ga" not in existing_names:
        rand_a = random.randint(100_000_000, 999_999_999)
        rand_b = random.randint(100_000_000, 999_999_999)
        ga_value = f"GA1.1.{rand_a}.{int(now - random.randint(86400 * 7, 86400 * 90))}"
        cookies.append({
            "name": "_ga",
            "value": ga_value,
            "domain": ".realtor.com",
            "path": "/",
            "expires": far_future,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        })

    if "ajs_anonymous_id" not in existing_names:
        anon_id = str(uuid.uuid4())
        cookies.append({
            "name": "ajs_anonymous_id",
            "value": anon_id,
            "domain": ".realtor.com",
            "path": "/",
            "expires": far_future,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        })

    return cookies


# ---------------------------------------------------------------------------
# CDP injection
# ---------------------------------------------------------------------------

def inject_donor_cookies(
    driver,
    *,
    profile_dirs: Optional[List[str]] = None,
    profile_subdir: str = "Default",
    keychain_service: str = "Chrome Safe Storage",
    include_synthetic: bool = True,
) -> int:
    """
    Extract safe realtor.com cookies from ``profile_dirs`` and inject them
    into ``driver`` via CDP Network.setCookie.

    Parameters
    ----------
    driver          Selenium WebDriver (must have ``execute_cdp_cmd``).
    profile_dirs    Paths to Chrome user-data-dirs to donate cookies from.
                    Falls back to ``_default_donor_profiles()`` when None.
    profile_subdir  Profile subdirectory inside each user-data-dir.
    keychain_service  macOS Keychain service name for decryption key.
    include_synthetic  Whether to add synthetic _ga / ajs_anonymous_id if the
                    donor set is missing them.

    Returns
    -------
    Number of cookies successfully injected.
    """
    if profile_dirs is None:
        profile_dirs = _default_donor_profiles()

    all_cookies: Dict[str, Dict] = {}  # name → cookie, deduplicated

    for profile_dir in profile_dirs:
        try:
            batch = read_realtor_cookies(
                profile_dir,
                profile_subdir=profile_subdir,
                keychain_service=keychain_service,
            )
            for c in batch:
                all_cookies.setdefault(c["name"], c)  # first donor wins
        except Exception as exc:
            print(f"[cookie-donor] Warning: could not read from {profile_dir}: {exc}", flush=True)

    if include_synthetic:
        for c in _synthetic_cookies(set(all_cookies.keys())):
            all_cookies.setdefault(c["name"], c)

    if not all_cookies:
        print("[cookie-donor] No cookies to inject.", flush=True)
        return 0

    injected = 0
    for cookie in all_cookies.values():
        try:
            driver.execute_cdp_cmd("Network.setCookie", cookie)
            injected += 1
        except Exception as exc:
            print(f"[cookie-donor] Failed to inject '{cookie['name']}': {exc}", flush=True)

    if injected:
        print(f"[cookie-donor] Injected {injected} cookies into browser session.", flush=True)
    return injected


# ---------------------------------------------------------------------------
# Profile discovery helpers
# ---------------------------------------------------------------------------

def _default_donor_profiles() -> List[str]:
    """Return user-data-dir paths for Chrome profiles that are likely to have
    realtor.com cookies based on the known profile layout on this machine."""
    chrome_base = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    if not chrome_base.exists():
        return []
    # Profiles confirmed to contain realtor.com cookies (from prior audit):
    # Profile 5, Profile 11, Profile 12, Profile 13 — return user-data-dir
    # (the profile_subdir parameter handles the subdirectory).
    return [str(chrome_base)]


def list_chrome_profiles(base_dir: Optional[str] = None) -> List[Dict]:
    """
    Enumerate Chrome profiles and report which ones have realtor.com cookies.

    Returns a list of dicts with keys: path, subdir, name, cookie_count.
    Useful for picking the right --realtor-cookie-donor-profiles argument.
    """
    if base_dir is None:
        base_dir = str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome")

    base = Path(base_dir)
    if not base.exists():
        return []

    profile_dirs = [d for d in base.iterdir() if d.is_dir() and (d.name == "Default" or d.name.startswith("Profile "))]

    results = []
    for profile_dir in sorted(profile_dirs, key=lambda p: p.name):
        for cookies_rel in ("Network/Cookies", "Cookies"):
            cookies_db = profile_dir / cookies_rel
            if not cookies_db.exists():
                continue
            try:
                conn = sqlite3.connect(f"file:{cookies_db}?mode=ro&immutable=1", uri=True)
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%realtor.com'"
                    ).fetchone()
                    count = row[0] if row else 0
                finally:
                    conn.close()
                if count > 0:
                    # Read display name from Preferences if available.
                    prefs = profile_dir / "Preferences"
                    display_name = profile_dir.name
                    if prefs.exists():
                        try:
                            import json
                            data = json.loads(prefs.read_text(errors="replace"))
                            display_name = data.get("profile", {}).get("name", profile_dir.name)
                        except Exception:
                            pass
                    results.append({
                        "path": str(base),
                        "subdir": profile_dir.name,
                        "name": display_name,
                        "cookie_count": count,
                    })
            except Exception:
                pass
            break  # Found the Cookies DB in one of the two locations; move on.

    return results
