"""
Manage scraper Chrome profile health.

Clears bot-detection state (PerimeterX/KPSDK cookies, LocalStorage, IndexedDB) and
seeds synthetic interactions (browsing history, analytics cookies) that make
profiles look like returning users rather than fresh bot sessions.

All SQLite operations require Chrome to NOT be running against the target profile.
Chrome holds exclusive locks on its databases while running.

Profile discovery
-----------------
Scans two roots:
  - ScraperDiagnostics/ParallelProfiles/   (side-by-side runner isolated sessions)
  - CHROME_USER_DATA_DIR                   (main scraper profile, env var)

Each root may contain either a flat user-data-dir (Chrome-style "Profile N"
subdirs) or a single profile directory that IS the user-data-dir itself (how the
parallel runner creates them, with one profile subdir named "Default").
"""
from __future__ import annotations

import fnmatch
import json
import re
import random
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from re_analyzer.utility.utility import DATA_PATH
    from re_analyzer.scrapers.scraping_utility import CHROME_USER_DATA_DIR
except Exception:
    DATA_PATH = str(Path(__file__).resolve().parents[2] / "Data")
    CHROME_USER_DATA_DIR = None

DATA_ROOT = Path(DATA_PATH)
DIAGNOSTICS_ROOT = DATA_ROOT / "ScraperDiagnostics"
PARALLEL_PROFILES_ROOT = DIAGNOSTICS_ROOT / "ParallelProfiles"

_CHROME_EPOCH_OFFSET_S = 11_644_473_600


def _to_chrome_us(unix_ts: float) -> int:
    return int((unix_ts + _CHROME_EPOCH_OFFSET_S) * 1_000_000)


# ── Cookie classification ─────────────────────────────────────────────────────

# Patterns for cookies that indicate a detected/blocked session.
_DETECTION_PATTERNS: Tuple[str, ...] = (
    "_pxvid",
    "_px*",
    "KP_UIDz*",
    "__bot*",
    "pxcts",
    "pxscts",
    "_dd_s",
    "_uab_popup",
    "__uc_optout",
)

# Names of synthetic analytics cookies we plant to look like a returning user.
_SYNTHETIC_NAMES = {"_ga", "_gcl_au", "ajs_anonymous_id", "panoramaId", "_rdt_uuid", "rdVisits"}

# Chrome cache subdirectory names inside a profile subdir.
_CACHE_DIRS = {"Cache", "Code Cache", "GPUCache"}

# Persistent tracking directories (LocalStorage, IndexedDB, Session Storage).
_TRACKING_DIRS = {"Local Storage", "IndexedDB", "Session Storage"}


def _name_is_detection(name: str) -> bool:
    lower = name.lower()
    for pattern in _DETECTION_PATTERNS:
        if fnmatch.fnmatchcase(lower, pattern.lower()) or fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def _natural_profile_key(name: str) -> tuple:
    match = re.match(r"^Profile\s+(\d+)$", str(name or ""))
    if str(name or "") == "Default":
        return (0, 0)
    if match:
        return (1, int(match.group(1)))
    return (2, str(name or "").lower())


def _read_json(path: Path) -> Dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _known_browser_root(path: Path) -> bool:
    raw = str(path.expanduser().resolve()).replace("\\", "/").lower()
    for root_str in _DONOR_CHROME_ROOTS:
        try:
            root = str(Path(root_str).expanduser().resolve()).replace("\\", "/").lower()
        except Exception:
            root = str(Path(root_str).expanduser()).replace("\\", "/").lower()
        if raw == root:
            return True
    return False


def _profile_info_cache(root: Path) -> Dict[str, Dict]:
    state = _read_json(root / "Local State")
    info = state.get("profile", {}).get("info_cache", {})
    return info if isinstance(info, dict) else {}


def _chrome_profile_metadata(root: Path, profile_subdir: str) -> Dict:
    info = _profile_info_cache(root).get(profile_subdir, {}) or {}
    prefs = _read_json(root / profile_subdir / "Preferences")
    pref_profile = prefs.get("profile", {}) if isinstance(prefs.get("profile"), dict) else {}
    name = info.get("name") or pref_profile.get("name") or profile_subdir
    user_name = info.get("user_name") or ""
    gaia_name = info.get("gaia_name") or ""
    avatar_icon = info.get("avatar_icon") or ""
    named = bool(info or pref_profile.get("name"))
    secondary = user_name or gaia_name
    display = f"{name} ({profile_subdir})" if name and name != profile_subdir else profile_subdir
    return {
        "chrome_name": name,
        "chrome_user_name": user_name,
        "chrome_gaia_name": gaia_name,
        "chrome_avatar_icon": avatar_icon,
        "profile_folder": profile_subdir,
        "is_named_chrome_profile": named,
        "profile_secondary_label": secondary,
        "chrome_display_name": display,
    }


def _discover_profile_subdirs(root: Path) -> List[str]:
    names = set(_profile_info_cache(root).keys())
    try:
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name == "Default" or entry.name.startswith("Profile "):
                if (entry / "Preferences").exists() or _find_cookies_db(root, entry.name):
                    names.add(entry.name)
    except OSError:
        pass
    return sorted(names, key=_natural_profile_key)


# ── Filesystem helpers ────────────────────────────────────────────────────────

def _dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _find_cookies_db(profile_dir: Path, profile_subdir: str) -> Optional[Path]:
    for rel in (f"{profile_subdir}/Network/Cookies", f"{profile_subdir}/Cookies"):
        p = profile_dir / rel
        if p.exists():
            return p
    return None


def _find_history_db(profile_dir: Path, profile_subdir: str) -> Optional[Path]:
    p = profile_dir / profile_subdir / "History"
    return p if p.exists() else None


# ── Per-profile scan ──────────────────────────────────────────────────────────

def _scan_detection_cookies(profile_dir: Path, profile_subdir: str) -> List[Dict]:
    db = _find_cookies_db(profile_dir, profile_subdir)
    if not db:
        return []
    found = []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            rows = conn.execute(
                "SELECT name, host_key FROM cookies WHERE host_key LIKE '%realtor.com'"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    for name, host_key in rows:
        if _name_is_detection(name):
            found.append({"name": name, "domain": host_key})
    return found


def _scan_synthetic_cookies(profile_dir: Path, profile_subdir: str) -> List[str]:
    db = _find_cookies_db(profile_dir, profile_subdir)
    if not db:
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            rows = conn.execute(
                "SELECT name FROM cookies WHERE host_key LIKE '%realtor.com'"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    present = {row[0] for row in rows}
    return sorted(present & _SYNTHETIC_NAMES)


def _scan_history(profile_dir: Path, profile_subdir: str) -> Dict:
    db = _find_history_db(profile_dir, profile_subdir)
    if not db:
        return {"seeded": False, "entry_count": 0}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM visits v JOIN urls u ON v.url = u.id "
                "WHERE u.url LIKE '%realtor.com%'"
            ).fetchone()
            count = row[0] if row else 0
        finally:
            conn.close()
        return {"seeded": count > 0, "entry_count": count}
    except Exception:
        return {"seeded": db.exists(), "entry_count": 0}


def scan_profile(
    profile_dir: str,
    profile_subdir: str = "Default",
    *,
    display_name: Optional[str] = None,
    source: str = "unknown",
    metadata: Optional[Dict] = None,
    manageable: bool = True,
) -> Dict:
    p = Path(profile_dir).expanduser().resolve()
    metadata = dict(metadata or {})
    detection = _scan_detection_cookies(p, profile_subdir)
    synthetic = _scan_synthetic_cookies(p, profile_subdir)
    history = _scan_history(p, profile_subdir)

    subdir_path = p / profile_subdir
    cache_bytes = sum(_dir_size(subdir_path / d) for d in _CACHE_DIRS if (subdir_path / d).exists())
    tracking_bytes = sum(_dir_size(subdir_path / d) for d in _TRACKING_DIRS if (subdir_path / d).exists())
    total_bytes = _dir_size(p)

    last_modified = None
    try:
        last_modified = p.stat().st_mtime
    except OSError:
        pass

    return {
        "profile_dir": str(p),
        "profile_subdir": profile_subdir,
        "display_name": display_name or metadata.get("chrome_display_name") or p.name,
        "profile_label": metadata.get("chrome_display_name") or display_name or p.name,
        "profile_secondary_label": metadata.get("profile_secondary_label") or "",
        "source": source,
        "manageable": manageable,
        "size_bytes": total_bytes,
        "cache_size_bytes": cache_bytes,
        "tracking_size_bytes": tracking_bytes,
        "detection_cookies": detection,
        "synthetic_cookies": synthetic,
        "history": history,
        "last_modified_ts": last_modified,
        **metadata,
    }


def scan_profiles(extra_dirs: Optional[List[str]] = None) -> List[Dict]:
    """Scan all known scraper profile directories and return their health status."""
    profiles: List[Dict] = []

    # Parallel profiles: each subdir is its own user-data-dir with a "Default" profile.
    if PARALLEL_PROFILES_ROOT.exists():
        for entry in sorted(PARALLEL_PROFILES_ROOT.iterdir()):
            if not entry.is_dir():
                continue
            default_path = entry / "Default"
            if not default_path.exists():
                continue
            provider = entry.name.split("_")[0] if "_" in entry.name else "unknown"
            profiles.append(scan_profile(
                str(entry), "Default",
                display_name=entry.name,
                source="parallel_profiles",
            ))

    # Main scraper user-data-dir: contains "Profile N" subdirectories.
    if CHROME_USER_DATA_DIR:
        root = Path(CHROME_USER_DATA_DIR).expanduser().resolve()
        if root.exists():
            is_browser_root = _known_browser_root(root)
            for subdir_name in _discover_profile_subdirs(root):
                # Each Profile N IS the profile subdir inside root.
                metadata = _chrome_profile_metadata(root, subdir_name)
                prefix = "browser" if is_browser_root else "main"
                profiles.append(scan_profile(
                    str(root), subdir_name,
                    display_name=f"{prefix} / {metadata.get('chrome_display_name') or subdir_name}",
                    source="browser_profiles" if is_browser_root else "main",
                    metadata=metadata,
                    manageable=not is_browser_root,
                ))

    # Caller-supplied extra dirs.
    for d in (extra_dirs or []):
        p = Path(d).expanduser().resolve()
        if p.exists():
            profiles.append(scan_profile(str(p), "Default", source="extra"))

    return profiles


# ── Operations ────────────────────────────────────────────────────────────────

def clear_detection_cookies(profile_dir: str, profile_subdir: str = "Default") -> Dict:
    """Delete PerimeterX/KPSDK cookies from the Cookies SQLite database."""
    p = Path(profile_dir).expanduser().resolve()
    db = _find_cookies_db(p, profile_subdir)
    if not db:
        return {"removed": 0, "skipped": 0, "error": "Cookies database not found"}
    try:
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT rowid, name FROM cookies WHERE host_key LIKE '%realtor.com'"
            ).fetchall()
            to_delete = [rowid for rowid, name in rows if _name_is_detection(name)]
            if to_delete:
                conn.executemany("DELETE FROM cookies WHERE rowid = ?", [(r,) for r in to_delete])
                conn.commit()
        finally:
            conn.close()
        return {"removed": len(to_delete), "skipped": len(rows) - len(to_delete)}
    except Exception as exc:
        return {"removed": 0, "skipped": 0, "error": str(exc)}


def clear_http_cache(profile_dir: str, profile_subdir: str = "Default") -> Dict:
    """Delete Chrome HTTP cache directories from the profile."""
    p = Path(profile_dir).expanduser().resolve()
    subdir = p / profile_subdir
    freed = 0
    removed = []
    errors = []
    for name in _CACHE_DIRS:
        d = subdir / name
        if not d.exists():
            continue
        size = _dir_size(d)
        try:
            shutil.rmtree(d)
            freed += size
            removed.append(name)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return {"freed_bytes": freed, "removed_dirs": removed, "errors": errors}


def clear_persistent_tracking(profile_dir: str, profile_subdir: str = "Default") -> Dict:
    """Delete LocalStorage, IndexedDB, and Session Storage directories."""
    p = Path(profile_dir).expanduser().resolve()
    subdir = p / profile_subdir
    freed = 0
    removed = []
    errors = []
    for name in _TRACKING_DIRS:
        d = subdir / name
        if not d.exists():
            continue
        size = _dir_size(d)
        try:
            shutil.rmtree(d)
            freed += size
            removed.append(name)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return {"freed_bytes": freed, "removed_dirs": removed, "errors": errors}


def seed_history(profile_dir: str, profile_subdir: str = "Default") -> Dict:
    """Seed synthetic realtor.com browsing history into the Chrome profile."""
    try:
        from re_analyzer.scrapers.realtor_profile_seeder import seed_chrome_profile
        n = seed_chrome_profile(profile_dir, profile_subdir=profile_subdir, overwrite=True)
        return {"visits_inserted": n}
    except Exception as exc:
        return {"visits_inserted": 0, "error": str(exc)}


def inject_synthetic_cookies(profile_dir: str, profile_subdir: str = "Default") -> Dict:
    """
    Write synthetic analytics cookies directly to the Chrome Cookies SQLite.

    Cookies are written as plaintext (value column, empty encrypted_value).
    Chrome reads plaintext value when encrypted_value is absent or empty — this
    is the same fallback path used for cookies set before Chrome's encryption
    scheme was introduced and for cookies set by renderers without the AES key.
    """
    p = Path(profile_dir).expanduser().resolve()
    db = _find_cookies_db(p, profile_subdir)
    if not db:
        # Create the Cookies DB at the standard path.
        db_dir = p / profile_subdir / "Network"
        db_dir.mkdir(parents=True, exist_ok=True)
        db = db_dir / "Cookies"

    now = time.time()
    far_future = int(now + 86_400 * 365 * 2)

    rand_a = random.randint(100_000_000, 999_999_999)
    rand_b = random.randint(100_000_000, 999_999_999)
    ga_ts = int(now - random.randint(86_400 * 7, 86_400 * 180))

    cookies_to_inject = [
        # (name, value, domain, path, expires_unix, is_secure, is_httponly, samesite)
        (
            "_ga",
            f"GA1.1.{rand_a}.{ga_ts}",
            ".realtor.com", "/", far_future, 1, 0, -1,
        ),
        (
            "ajs_anonymous_id",
            str(uuid.uuid4()),
            ".realtor.com", "/", far_future, 1, 0, -1,
        ),
        (
            "_gcl_au",
            f"1.1.{random.randint(100_000_000, 999_999_999)}.{int(now - random.randint(0, 86_400 * 30))}",
            ".realtor.com", "/", int(now + 86_400 * 90), 1, 0, -1,
        ),
        (
            "panoramaId",
            uuid.uuid4().hex,
            ".realtor.com", "/", far_future, 1, 0, -1,
        ),
        (
            "_rdt_uuid",
            f"{int(now * 1000)}.{uuid.uuid4().hex[:24]}",
            ".realtor.com", "/", int(now + 86_400 * 365), 1, 0, -1,
        ),
        (
            "rdVisits",
            str(random.randint(4, 28)),
            ".realtor.com", "/", int(now + 86_400 * 30), 0, 0, 1,
        ),
    ]

    try:
        conn = sqlite3.connect(str(db))
        try:
            _ensure_cookies_schema(conn)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(cookies)").fetchall()}
            injected = 0
            skipped = 0
            for i, (name, value, domain, path, expires, is_secure, is_httponly, samesite) in enumerate(cookies_to_inject):
                creation_us = _to_chrome_us(now) + i
                expires_us = _to_chrome_us(expires)
                now_us = _to_chrome_us(now)
                # Check if already present to avoid duplicates.
                existing = conn.execute(
                    "SELECT rowid FROM cookies WHERE host_key = ? AND name = ? AND path = ?",
                    (domain, name, path),
                ).fetchone()
                if existing:
                    skipped += 1
                    continue
                row_data: Dict = {
                    "creation_utc": creation_us,
                    "host_key": domain,
                    "name": name,
                    "value": value,
                    "encrypted_value": b"",
                    "path": path,
                    "expires_utc": expires_us,
                    "is_secure": is_secure,
                    "is_httponly": is_httponly,
                    "last_access_utc": now_us,
                    "has_expires": 1,
                    "is_persistent": 1,
                    "priority": 1,
                    "samesite": samesite,
                }
                if "source_scheme" in columns:
                    row_data["source_scheme"] = 2 if is_secure else 1
                if "source_port" in columns:
                    row_data["source_port"] = 443 if is_secure else 80
                if "last_update_utc" in columns:
                    row_data["last_update_utc"] = now_us
                if "top_frame_site_key" in columns:
                    row_data["top_frame_site_key"] = ""
                if "is_same_party" in columns:
                    row_data["is_same_party"] = 0
                if "source_type" in columns:
                    row_data["source_type"] = 0
                if "has_cross_site_ancestor" in columns:
                    row_data["has_cross_site_ancestor"] = 0
                col_list = ", ".join(row_data.keys())
                placeholders = ", ".join("?" * len(row_data))
                conn.execute(
                    f"INSERT OR IGNORE INTO cookies ({col_list}) VALUES ({placeholders})",
                    list(row_data.values()),
                )
                injected += 1
            conn.commit()
        finally:
            conn.close()
        return {"injected": injected, "skipped_existing": skipped}
    except Exception as exc:
        return {"injected": 0, "skipped_existing": 0, "error": str(exc)}


def _ensure_cookies_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key   LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY,
            value LONGVARCHAR
        );
        CREATE TABLE IF NOT EXISTS cookies (
            creation_utc     INTEGER NOT NULL UNIQUE PRIMARY KEY,
            host_key         TEXT NOT NULL,
            name             TEXT NOT NULL,
            value            TEXT NOT NULL DEFAULT '',
            encrypted_value  BLOB NOT NULL DEFAULT '',
            path             TEXT NOT NULL,
            expires_utc      INTEGER NOT NULL,
            is_secure        INTEGER NOT NULL,
            is_httponly      INTEGER NOT NULL,
            last_access_utc  INTEGER NOT NULL,
            has_expires      INTEGER NOT NULL DEFAULT 1,
            is_persistent    INTEGER NOT NULL DEFAULT 1,
            priority         INTEGER NOT NULL DEFAULT 1,
            samesite         INTEGER NOT NULL DEFAULT -1
        );
        INSERT OR IGNORE INTO meta (key, value) VALUES ('version', '24');
        INSERT OR IGNORE INTO meta (key, value) VALUES ('last_compatible_version', '9');
    """)


# ── Batch operation dispatcher ────────────────────────────────────────────────

_KNOWN_OPERATIONS = {
    "clear_detection_cookies",
    "clear_http_cache",
    "clear_persistent_tracking",
    "seed_history",
    "inject_synthetic_cookies",
}


def apply_operations(
    profile_dir: str,
    profile_subdir: str,
    operations: List[str],
) -> Dict:
    """Apply a list of operations to a single profile. Returns per-operation results."""
    results: Dict[str, Dict] = {}
    for op in operations:
        if op not in _KNOWN_OPERATIONS:
            results[op] = {"error": f"unknown operation '{op}'"}
            continue
        if op == "clear_detection_cookies":
            results[op] = clear_detection_cookies(profile_dir, profile_subdir)
        elif op == "clear_http_cache":
            results[op] = clear_http_cache(profile_dir, profile_subdir)
        elif op == "clear_persistent_tracking":
            results[op] = clear_persistent_tracking(profile_dir, profile_subdir)
        elif op == "seed_history":
            results[op] = seed_history(profile_dir, profile_subdir)
        elif op == "inject_synthetic_cookies":
            results[op] = inject_synthetic_cookies(profile_dir, profile_subdir)
    return results


# ── Donor cookie copy ─────────────────────────────────────────────────────────

# macOS Chrome / Chromium user-data-dir roots to search for donor profiles.
_DONOR_CHROME_ROOTS: Tuple[str, ...] = (
    "~/Library/Application Support/Google/Chrome",
    "~/Library/Application Support/Google/Chrome Beta",
    "~/Library/Application Support/Google/Chrome Canary",
    "~/Library/Application Support/Chromium",
    "~/Library/Application Support/Microsoft Edge",
)


def find_donor_profiles(*, include_empty: bool = False) -> List[Dict]:
    """Scan real browser installations for Chrome profiles that have realtor.com cookies.

    Same-machine encrypted_value bytes are portable — the AES-128-CBC key is
    derived from the macOS Keychain entry 'Chrome Safe Storage', which is shared
    across all Chrome profiles owned by the same OS user account.  Donor rows
    can be copied directly without decryption.

    Returns a list of candidate dicts, sorted by cookie_count descending.
    Profiles with many detection cookies are flagged but still included so the
    operator can make an informed choice.
    """
    candidates = []
    seen: set = set()
    for root_str in _DONOR_CHROME_ROOTS:
        root = Path(root_str).expanduser()
        if not root.exists():
            continue
        for subdir_name in _discover_profile_subdirs(root):
            db = _find_cookies_db(root, subdir_name)
            if (not db or not db.exists()) and not include_empty:
                continue
            key = f"{root.resolve()}::{subdir_name}"
            if key in seen:
                continue
            seen.add(key)
            rows = []
            try:
                if db and db.exists():
                    conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
                    try:
                        rows = conn.execute(
                            "SELECT name, LENGTH(encrypted_value) > 0 "
                            "FROM cookies WHERE host_key LIKE '%realtor.com'"
                        ).fetchall()
                    finally:
                        conn.close()
            except Exception:
                continue
            if not rows and not include_empty:
                continue
            detection = sum(1 for r in rows if _name_is_detection(r[0]))
            analytics = sum(1 for r in rows if r[0] in _SYNTHETIC_NAMES)
            encrypted_count = sum(1 for r in rows if r[1])
            metadata = _chrome_profile_metadata(root, subdir_name)
            browser_name = root.name.replace("Chrome", "Google Chrome")
            display_name = metadata.get("chrome_display_name") or f"{browser_name} / {subdir_name}"
            candidates.append({
                "profile_dir": str(root),
                "profile_subdir": subdir_name,
                "display_name": f"{browser_name} / {display_name}",
                "profile_label": display_name,
                "profile_secondary_label": metadata.get("profile_secondary_label") or "",
                "browser_name": browser_name,
                **metadata,
                "cookie_count": len(rows),
                "analytics_cookies": analytics,
                "detection_cookies": detection,
                "encrypted_count": encrypted_count,
                "is_blocked": detection > 0,
                "has_realtor_cookies": len(rows) > 0,
                "copyable": len(rows) > 0,
            })

    candidates.sort(key=lambda c: (
        not c.get("copyable", False),
        c["is_blocked"],
        -c["analytics_cookies"],
        -c["cookie_count"],
        str(c.get("profile_label") or c.get("display_name") or "").lower(),
    ))
    return candidates


def copy_donor_cookies(
    donor_profile_dir: str,
    donor_profile_subdir: str,
    dest_profile_dir: str,
    dest_profile_subdir: str = "Default",
    *,
    skip_detection: bool = True,
) -> Dict:
    """Copy realtor.com cookies from a donor Chrome profile into a scraper profile.

    Encrypted cookie values (encrypted_value column) are copied verbatim.  This
    works without any AES library because both profiles live on the same macOS user
    account: Chrome derives its cookie encryption key from the macOS Keychain entry
    'Chrome Safe Storage', which is the same key for every Chrome profile belonging
    to the same OS user.  The destination profile's Chrome instance will decrypt
    the copied bytes transparently.

    When skip_detection=True (default), PerimeterX / KPSDK cookies are excluded
    so we don't copy over a blocked session state.
    """
    donor_p = Path(donor_profile_dir).expanduser().resolve()
    donor_db = _find_cookies_db(donor_p, donor_profile_subdir)
    if not donor_db or not donor_db.exists():
        return {"copied": 0, "error": "Donor cookies database not found"}

    dest_p = Path(dest_profile_dir).expanduser().resolve()
    dest_db = _find_cookies_db(dest_p, dest_profile_subdir)
    if not dest_db:
        db_dir = dest_p / dest_profile_subdir / "Network"
        db_dir.mkdir(parents=True, exist_ok=True)
        dest_db = db_dir / "Cookies"

    # Read donor rows (read-only, immutable).
    try:
        donor_conn = sqlite3.connect(f"file:{donor_db}?mode=ro&immutable=1", uri=True)
        try:
            col_names = [row[1] for row in donor_conn.execute("PRAGMA table_info(cookies)").fetchall()]
            if not col_names:
                return {"copied": 0, "error": "Could not read donor schema"}
            name_idx = col_names.index("name") if "name" in col_names else None
            rows = donor_conn.execute(
                f"SELECT {', '.join(col_names)} FROM cookies WHERE host_key LIKE '%realtor.com'"
            ).fetchall()
        finally:
            donor_conn.close()
    except Exception as exc:
        return {"copied": 0, "error": f"Failed to read donor: {exc}"}

    total_read = len(rows)
    if skip_detection and name_idx is not None:
        rows = [r for r in rows if not _name_is_detection(r[name_idx])]

    if not rows:
        return {"copied": 0, "skipped_detection": total_read - len(rows), "total_read": total_read}

    # Write to destination profile.
    try:
        dest_conn = sqlite3.connect(str(dest_db))
        try:
            _ensure_cookies_schema(dest_conn)
            # Add any donor columns absent from dest schema.
            dest_cols = {row[1] for row in dest_conn.execute("PRAGMA table_info(cookies)").fetchall()}
            for col in col_names:
                if col not in dest_cols:
                    try:
                        dest_conn.execute(f"ALTER TABLE cookies ADD COLUMN {col} BLOB DEFAULT ''")
                    except Exception:
                        pass
            # Re-read dest columns after potential ALTER.
            dest_cols = {row[1] for row in dest_conn.execute("PRAGMA table_info(cookies)").fetchall()}
            valid_cols = [c for c in col_names if c in dest_cols]
            valid_indices = [i for i, c in enumerate(col_names) if c in dest_cols]
            col_list = ", ".join(valid_cols)
            placeholders = ", ".join("?" * len(valid_cols))
            copied = 0
            for row in rows:
                filtered = [row[i] for i in valid_indices]
                dest_conn.execute(
                    f"INSERT OR REPLACE INTO cookies ({col_list}) VALUES ({placeholders})",
                    filtered,
                )
                copied += 1
            dest_conn.commit()
        finally:
            dest_conn.close()
        return {
            "copied": copied,
            "skipped_detection": total_read - len(rows),
            "total_read": total_read,
        }
    except Exception as exc:
        return {"copied": 0, "error": f"Failed to write cookies: {exc}"}
