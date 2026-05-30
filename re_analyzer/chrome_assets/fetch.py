from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional


def _read_json(url: str, timeout_seconds: int = 20) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _download(url: str, dest: Path, timeout_seconds: int = 120) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)


def _zip_extract(zip_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)


def _mac_strip_quarantine(path: Path) -> None:
    if platform.system() != "Darwin":
        return
    try:
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return


def _detect_platform_label() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        return "mac-arm64" if machine in {"arm64", "aarch64"} else "mac-x64"
    if system == "Linux":
        if machine in {"arm64", "aarch64"}:
            return "linux-arm64"
        return "linux64"
    if system == "Windows":
        return "win64"
    raise RuntimeError(f"Unsupported platform: {system} ({machine})")


def _pick_download(downloads: list[dict], *, platform_label: str) -> Optional[dict]:
    for entry in downloads or []:
        if str(entry.get("platform") or "").strip() == platform_label:
            return entry
    return None


def _chrome_assets_root() -> Path:
    # re_analyzer/chrome_assets/fetch.py -> re_analyzer -> ChromeAssets
    return Path(__file__).resolve().parents[1] / "ChromeAssets"


def _install_chrome_for_testing(zip_path: Path, *, version: str, platform_label: str) -> Path:
    assets = _chrome_assets_root()
    dest_dir = assets / ".downloads" / "chrome" / f"{platform_label}-{version}"
    staging = dest_dir / "_staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    _zip_extract(zip_path, staging)

    # Chrome for Testing zips contain an .app on mac, and a "chrome-*/chrome" layout elsewhere.
    app_candidates = list(staging.glob("**/*.app"))
    if app_candidates:
        app_src = app_candidates[0]
        app_dest = assets / "Google Chrome for Testing.app"
        if app_dest.exists():
            shutil.rmtree(app_dest, ignore_errors=True)
        shutil.move(str(app_src), str(app_dest))
        _mac_strip_quarantine(app_dest)
        # Some extraction/move paths can drop executable bits on helper binaries.
        # Ensure the main binary + helper tools (e.g. crashpad handler) are executable.
        try:
            candidates = []
            candidates.extend((app_dest / "Contents" / "MacOS").glob("*"))
            candidates.extend(app_dest.glob("**/*.app/Contents/MacOS/*"))
            candidates.extend((app_dest / "Contents" / "Frameworks").glob("**/Helpers/*"))
            candidates.extend((app_dest / "Contents" / "Frameworks").glob("**/Helpers/*/*"))
            for item in candidates:
                try:
                    if item.is_file():
                        os.chmod(item, 0o755)
                except Exception:
                    continue
        except Exception:
            pass
        return app_dest

    chrome_bin = next(iter(staging.glob("**/chrome")), None)
    if not chrome_bin:
        raise RuntimeError("Unable to locate extracted chrome binary.")
    chrome_dest = assets / "chrome"
    chrome_dest.parent.mkdir(parents=True, exist_ok=True)
    if chrome_dest.exists():
        chrome_dest.unlink()
    shutil.copy2(chrome_bin, chrome_dest)
    os.chmod(chrome_dest, 0o755)
    return chrome_dest


def _install_chromedriver(zip_path: Path, *, version: str, platform_label: str) -> Path:
    assets = _chrome_assets_root()
    dest_dir = assets / ".downloads" / "chromedriver" / f"{platform_label}-{version}"
    staging = dest_dir / "_staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    _zip_extract(zip_path, staging)

    driver_bin = next(iter(staging.glob("**/chromedriver")), None)
    if not driver_bin:
        raise RuntimeError("Unable to locate extracted chromedriver binary.")

    # Copy to the flat path that the repo already prefers.
    driver_dest = assets / "chromedriver"
    if driver_dest.exists():
        try:
            driver_dest.unlink()
        except IsADirectoryError:
            shutil.rmtree(driver_dest, ignore_errors=True)
    shutil.copy2(driver_bin, driver_dest)
    os.chmod(driver_dest, 0o755)
    _mac_strip_quarantine(driver_dest)
    return driver_dest


def fetch(channel: str, version: str | None, platform_label: str | None) -> dict:
    platform_label = platform_label or _detect_platform_label()
    channel = (channel or "stable").strip().lower()
    if channel not in {"stable", "beta", "dev", "canary"}:
        raise RuntimeError("channel must be one of: stable, beta, dev, canary")

    channel_key = channel.capitalize()

    if version:
        meta_url = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"
        meta = _read_json(meta_url)
        versions = meta.get("versions") or []
        version = str(version).strip()
        record = next((v for v in versions if str(v.get("version")) == version), None)
        if not record:
            # Allow prefix matching such as "148" or "148.0.7778" by selecting the
            # most recent known-good entry with that prefix.
            prefix = version
            matches = [v for v in versions if str(v.get("version") or "").startswith(prefix)]
            if matches:
                record = matches[-1]
                version = str(record.get("version") or "").strip()
        if not record:
            raise RuntimeError(f"Chrome for Testing version not found in known-good list: {version}")
    else:
        meta_url = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
        meta = _read_json(meta_url)
        record = ((meta.get("channels") or {}).get(channel_key) or {})
        version = str(record.get("version") or "").strip()
        if not version:
            raise RuntimeError(f"Unable to resolve version for channel: {channel}")

    downloads = record.get("downloads") or {}
    chrome_entry = _pick_download(downloads.get("chrome") or [], platform_label=platform_label)
    driver_entry = _pick_download(downloads.get("chromedriver") or [], platform_label=platform_label)
    if not chrome_entry or not driver_entry:
        raise RuntimeError(f"Downloads missing for platform={platform_label} version={version}")

    assets = _chrome_assets_root()
    downloads_dir = assets / ".downloads" / "zips"
    chrome_zip = downloads_dir / f"chrome-{platform_label}-{version}.zip"
    driver_zip = downloads_dir / f"chromedriver-{platform_label}-{version}.zip"

    with tempfile.TemporaryDirectory(prefix="cft_fetch_") as _tmp:
        _download(str(chrome_entry["url"]), chrome_zip)
        _download(str(driver_entry["url"]), driver_zip)

    chrome_path = _install_chrome_for_testing(chrome_zip, version=version, platform_label=platform_label)
    driver_path = _install_chromedriver(driver_zip, version=version, platform_label=platform_label)
    return {
        "channel": channel,
        "version": version,
        "platform": platform_label,
        "chrome_path": str(chrome_path),
        "chromedriver_path": str(driver_path),
        "meta_url": meta_url,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Chrome for Testing + matching chromedriver into ChromeAssets.")
    parser.add_argument("--channel", default="stable", help="stable|beta|dev|canary (used when --version is omitted).")
    parser.add_argument("--version", default="", help="Exact Chrome for Testing version (overrides --channel).")
    parser.add_argument("--platform", default="", help="Platform label (e.g. mac-arm64, mac-x64, linux64, win64).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    result = fetch(
        channel=args.channel,
        version=str(args.version or "").strip() or None,
        platform_label=str(args.platform or "").strip() or None,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
