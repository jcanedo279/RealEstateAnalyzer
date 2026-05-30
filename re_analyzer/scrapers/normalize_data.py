"""
Normalizes scraped data into a single canonical Parquet store.

Three operations (all idempotent, safe to re-run):

  1. ARCHIVE  – moves legacy SearchResults/ and SearchResultsMetadata/ into Data/Archive/
  2. PRUNE    – keeps only the latest timestamped JSON per provider/ZIP; deletes older copies
  3. BUILD    – reads all latest canonical_listings_*.json files and writes
                Data/Canonical/canonical_listings.parquet

Run directly:
    ./venv/bin/python -m re_analyzer.scrapers.normalize_data [--dry-run] [--skip-archive]
                                                              [--skip-prune] [--skip-build]
                                                              [--output PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_ROOT = Path(__file__).resolve().parents[1] / "Data"
FETCHED_ROOT = DATA_ROOT / "Fetched"
CANONICAL_DIR = DATA_ROOT / "Canonical"
ARCHIVE_ROOT = DATA_ROOT / "Archive"

KNOWN_PROVIDERS = ("zillow", "redfin", "realtor")

_DEFAULT_MORTGAGE_RATE = 6.204
_FL_INSURANCE_RATE = 0.0035  # ~0.35 % of home value annually, rough FL average


# ---------------------------------------------------------------------------
# Helpers: field extraction
# ---------------------------------------------------------------------------

def _image_url(listing: dict) -> str:
    raw = listing.get("raw") or {}
    if isinstance(raw, dict):
        for key in ("imgSrc", "imgUrl", "image_url", "thumbnail", "photoLink", "photo_url"):
            val = raw.get(key)
            if val:
                return str(val)
        primary = raw.get("primary_photo") or {}
        if isinstance(primary, dict) and primary.get("href"):
            return str(primary["href"])
    return ""


def _tax_rate(listing: dict) -> float:
    tax_history = listing.get("tax_history")
    if isinstance(tax_history, list) and tax_history:
        entry = tax_history[0]
        if isinstance(entry, dict):
            paid = entry.get("taxPaid") or 0
            value = entry.get("value") or 0
            if paid and value and float(value) > 0:
                return round(float(paid) / float(value) * 100, 4)
    return 1.0  # FL average fallback


def _hoa(listing: dict) -> float:
    raw = listing.get("raw") or {}
    if isinstance(raw, dict):
        for key in ("hoaFee", "monthly_hoa", "monthlyHoa", "hoa", "hoaMonthly"):
            val = raw.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
    return 0.0


def _insurance(listing: dict) -> float:
    raw = listing.get("raw") or {}
    if isinstance(raw, dict):
        for key in ("annualHomeownersInsurance", "homeInsurance", "annualInsurance"):
            val = raw.get(key)
            if val is not None:
                try:
                    return round(float(val) / 12, 2)
                except (TypeError, ValueError):
                    pass
    price = listing.get("price") or 0
    if price:
        return round(float(price) * _FL_INSURANCE_RATE / 12, 2)
    return 0.0


def _safe_int(val) -> int:
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def _safe_float(val) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def listing_to_row(listing: dict, scraped_at: str) -> dict:
    price = _safe_float(listing.get("price"))
    rent = _safe_float(listing.get("rent_estimate"))
    grm = round(price / (rent * 12), 4) if rent > 0 and price > 0 else 0.0

    return {
        "canonical_property_id": str(listing.get("canonical_property_id") or ""),
        "source_name": str(listing.get("source_name") or ""),
        "source_property_id": str(listing.get("source_property_id") or ""),
        "street_address": str(listing.get("address") or ""),
        "city": str(listing.get("city") or ""),
        "state": str(listing.get("state") or "FL"),
        "zip_code": _safe_int(listing.get("zip_code")),
        "purchase_price": price,
        "monthly_restimate": rent,
        "gross_rent_multiplier": grm,
        "year_built": _safe_int(listing.get("year_built")),
        "bedrooms": _safe_int(listing.get("beds")),
        "bathrooms": _safe_float(listing.get("baths")),
        "annual_property_tax_rate": _tax_rate(listing),
        "living_area": _safe_int(listing.get("living_area")),
        "lot_size": _safe_int(listing.get("lot_size")),
        "home_type": str(listing.get("home_type") or "SINGLE_FAMILY"),
        "annual_mortgage_rate": _DEFAULT_MORTGAGE_RATE,
        "monthly_homeowners_insurance": _insurance(listing),
        "monthly_hoa": _hoa(listing),
        "latitude": _safe_float(listing.get("latitude")),
        "longitude": _safe_float(listing.get("longitude")),
        "property_url": str(listing.get("url") or ""),
        "image_url": _image_url(listing),
        "home_features_score": 0.0,
        "is_waterfront": "False",
        "listing_status": str(listing.get("status") or "Active"),
        "scraped_at": scraped_at,
    }


# ---------------------------------------------------------------------------
# Step 1: Archive legacy directories
# ---------------------------------------------------------------------------

_LEGACY_DIRS = ["SearchResults", "SearchResultsMetadata"]


def archive_legacy(data_root: Path = DATA_ROOT, dry_run: bool = False) -> dict:
    archive_root = data_root / "Archive"
    results = {}
    for name in _LEGACY_DIRS:
        src = data_root / name
        if not src.exists():
            results[name] = "skipped (not found)"
            continue
        dst = archive_root / name
        if dst.exists():
            results[name] = f"skipped (archive already exists at Archive/{name})"
            continue
        print(f"  {'[dry-run] ' if dry_run else ''}archiving {name}/ → Archive/{name}/")
        if not dry_run:
            archive_root.mkdir(exist_ok=True)
            shutil.move(str(src), str(dst))
        results[name] = "archived" if not dry_run else "would archive"
    return results


# ---------------------------------------------------------------------------
# Step 2: Prune old timestamped JSON files
# ---------------------------------------------------------------------------

def _prune_zip_dir(zip_dir: Path, dry_run: bool) -> int:
    """Keep only the newest canonical and raw listing files; return count deleted."""
    deleted = 0
    for pattern in ("canonical_listings_*.json", "listings_*.json"):
        files = sorted(zip_dir.glob(pattern), reverse=True)
        for old in files[1:]:
            if not dry_run:
                old.unlink()
            deleted += 1
    return deleted


def prune_old_json(fetched_root: Path = FETCHED_ROOT, dry_run: bool = False) -> dict:
    total_deleted = 0
    total_bytes = 0
    for provider in KNOWN_PROVIDERS:
        provider_dir = fetched_root / provider
        if not provider_dir.exists():
            continue
        for zip_dir in provider_dir.iterdir():
            if not zip_dir.is_dir() or not zip_dir.name.isdigit():
                continue
            for pattern in ("canonical_listings_*.json", "listings_*.json"):
                files = sorted(zip_dir.glob(pattern), reverse=True)
                for old in files[1:]:
                    total_bytes += old.stat().st_size
                    total_deleted += 1
                    if not dry_run:
                        old.unlink()

    print(
        f"  {'[dry-run] ' if dry_run else ''}pruned {total_deleted} old JSON files "
        f"({total_bytes / 1024 / 1024:.1f} MB freed)"
    )
    return {"deleted": total_deleted if not dry_run else 0, "freed_mb": round(total_bytes / 1024 / 1024, 1)}


# ---------------------------------------------------------------------------
# Step 3: Build canonical Parquet
# ---------------------------------------------------------------------------

def _latest_canonical_files(fetched_root: Path = FETCHED_ROOT):
    """Yield (provider, zip_code, path, scraped_at) for each latest canonical JSON."""
    for provider in KNOWN_PROVIDERS:
        provider_dir = fetched_root / provider
        if not provider_dir.exists():
            continue
        for zip_dir in provider_dir.iterdir():
            if not zip_dir.is_dir() or not zip_dir.name.isdigit():
                continue
            files = sorted(zip_dir.glob("canonical_listings_*.json"), reverse=True)
            if files:
                # Extract timestamp from filename: canonical_listings_YYYY-MM-DD_HH-MM.json
                stem = files[0].stem  # e.g. canonical_listings_2026-05-28_19-35
                ts_part = stem.replace("canonical_listings_", "")
                try:
                    scraped_at = datetime.strptime(ts_part, "%Y-%m-%d_%H-%M").isoformat()
                except ValueError:
                    scraped_at = datetime.now().isoformat()
                yield provider, zip_dir.name, files[0], scraped_at


def build_canonical_parquet(
    fetched_root: Path = FETCHED_ROOT,
    output_path: Optional[Path] = None,
    dry_run: bool = False,
    extra_output_paths: Optional[list] = None,
) -> dict:
    try:
        import pandas as pd
    except ImportError:
        print("  pandas not available — skipping Parquet build")
        return {"error": "pandas not available"}

    if output_path is None:
        output_path = CANONICAL_DIR / "canonical_listings.parquet"

    rows = []
    file_count = 0
    error_count = 0

    for provider, zip_code, path, scraped_at in _latest_canonical_files(fetched_root):
        try:
            with open(path, encoding="utf-8") as fh:
                listings = json.load(fh)
            if not isinstance(listings, list):
                continue
            for listing in listings:
                rows.append(listing_to_row(listing, scraped_at))
            file_count += 1
        except Exception as exc:
            print(f"  warning: could not read {path}: {exc}")
            error_count += 1

    if not rows:
        print("  no canonical listings found — Parquet not written")
        return {"rows": 0, "files": file_count, "errors": error_count}

    df = pd.DataFrame(rows)

    # Deduplicate: keep one row per canonical_property_id (latest scraped_at wins)
    if "canonical_property_id" in df.columns and df["canonical_property_id"].str.len().gt(0).any():
        df = df.sort_values("scraped_at", ascending=False).drop_duplicates(
            subset=["canonical_property_id"], keep="first"
        )

    # Remove rows with no price and no address (junk entries)
    df = df[df["purchase_price"].gt(0) | df["street_address"].str.len().gt(0)]

    print(f"  {'[dry-run] ' if dry_run else ''}building Parquet: {len(df):,} rows from {file_count} files")

    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(output_path, index=False, compression="snappy")
        print(f"  written → {output_path}")

        for extra in (extra_output_paths or []):
            extra_path = Path(extra)
            extra_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(extra_path, index=False, compression="snappy")
            print(f"  written → {extra_path}")

    return {
        "rows": len(df),
        "files_read": file_count,
        "errors": error_count,
        "output": str(output_path) if not dry_run else None,
    }


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------

def normalize(
    data_root: Path = DATA_ROOT,
    fetched_root: Path = FETCHED_ROOT,
    output_path: Optional[Path] = None,
    extra_output_paths: Optional[list] = None,
    dry_run: bool = False,
    skip_archive: bool = False,
    skip_prune: bool = False,
    skip_build: bool = False,
) -> dict:
    results: dict = {}

    if not skip_archive:
        print("\n[1/3] Archiving legacy directories …")
        results["archive"] = archive_legacy(data_root=data_root, dry_run=dry_run)

    if not skip_prune:
        print("\n[2/3] Pruning old timestamped JSON files …")
        results["prune"] = prune_old_json(fetched_root=fetched_root, dry_run=dry_run)

    if not skip_build:
        print("\n[3/3] Building canonical Parquet …")
        results["build"] = build_canonical_parquet(
            fetched_root=fetched_root,
            output_path=output_path,
            dry_run=dry_run,
            extra_output_paths=extra_output_paths,
        )

    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without making changes")
    parser.add_argument("--skip-archive", action="store_true", help="Skip archiving legacy directories")
    parser.add_argument("--skip-prune", action="store_true", help="Skip pruning old JSON timestamps")
    parser.add_argument("--skip-build", action="store_true", help="Skip building the canonical Parquet")
    parser.add_argument("--output", type=Path, default=None, help="Override canonical Parquet output path")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    print("=== normalize_data ===")
    results = normalize(
        dry_run=args.dry_run,
        skip_archive=args.skip_archive,
        skip_prune=args.skip_prune,
        skip_build=args.skip_build,
        output_path=args.output,
    )
    print("\nDone:", results)
