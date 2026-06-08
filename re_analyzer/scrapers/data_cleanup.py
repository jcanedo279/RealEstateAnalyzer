"""
Show and optionally delete locally stored temporary/stale data.

Cleanup targets
---------------
  chrome_profiles  – Chrome user-data-dir caches under ScraperDiagnostics/ParallelProfiles/
  diagnostics      – Per-run JSON snapshot files in ScraperDiagnostics/
  old_listings     – Older timestamped listings_*.json / canonical_listings_*.json when a
                     newer version exists for the same provider+ZIP
  fetched_data     – ZIP-level listing directories under Data/Fetched/{provider}/
                     (use --older-than-days N to restrict to stale ZIPs only)
  archive          – Data/_property_reset_archive/ (legacy archived data)

Dry-run by default — pass --execute to actually delete.

Usage
-----
    python -m re_analyzer.scrapers.data_cleanup
    python -m re_analyzer.scrapers.data_cleanup --execute
    python -m re_analyzer.scrapers.data_cleanup --target chrome_profiles old_listings
    python -m re_analyzer.scrapers.data_cleanup --target fetched_data --older-than-days 30
    python -m re_analyzer.scrapers.data_cleanup --target fetched_data --older-than-days 30 --execute
    python -m re_analyzer.scrapers.data_cleanup --json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from re_analyzer.utility.utility import DATA_PATH

DATA_ROOT = Path(DATA_PATH)
FETCHED_ROOT = DATA_ROOT / "Fetched"
DIAGNOSTICS_ROOT = DATA_ROOT / "ScraperDiagnostics"
PARALLEL_PROFILES_ROOT = DIAGNOSTICS_ROOT / "ParallelProfiles"
ARCHIVE_ROOT = DATA_ROOT / "_property_reset_archive"
KNOWN_PROVIDERS = ["zillow", "redfin", "realtor"]
ALL_TARGETS = ["chrome_profiles", "diagnostics", "old_listings", "fetched_data", "archive"]


# ── Size / time helpers ───────────────────────────────────────────────────────

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


def _fmt_size(n_bytes: int) -> str:
    for unit, threshold in [("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)]:
        if n_bytes >= threshold:
            return f"{n_bytes / threshold:.1f} {unit}"
    return f"{n_bytes} B"


def _newest_listing_mtime(zip_dir: Path) -> Optional[float]:
    """Return the mtime of the newest canonical or raw listing file in a ZIP dir."""
    best: Optional[float] = None
    for pattern in ("canonical_listings_*.json", "listings_*.json"):
        for f in zip_dir.glob(pattern):
            try:
                mtime = f.stat().st_mtime
                if best is None or mtime > best:
                    best = mtime
            except OSError:
                pass
    return best


def _age_days(mtime: float) -> float:
    return (time.time() - mtime) / 86400


# ── Target scanners ──────────────────────────────────────────────────────────

class CleanTarget(NamedTuple):
    name: str
    label: str
    description: str
    size_bytes: int
    item_count: int
    item_label: str
    paths: List[Path]
    # Extra info for fetched_data (list of (provider, zip, age_days))
    freshness_detail: List[Tuple[str, str, float]]


def _scan_chrome_profiles() -> CleanTarget:
    paths: List[Path] = []
    if PARALLEL_PROFILES_ROOT.exists():
        for entry in sorted(PARALLEL_PROFILES_ROOT.iterdir()):
            if entry.is_dir():
                paths.append(entry)
    total = sum(_dir_size(p) for p in paths)
    return CleanTarget(
        name="chrome_profiles",
        label="Chrome profile caches",
        description=f"ScraperDiagnostics/ParallelProfiles/ ({len(paths)} dirs)",
        size_bytes=total, item_count=len(paths), item_label="dirs",
        paths=paths, freshness_detail=[],
    )


def _scan_diagnostics() -> CleanTarget:
    paths: List[Path] = []
    if DIAGNOSTICS_ROOT.exists():
        for entry in sorted(DIAGNOSTICS_ROOT.iterdir()):
            if entry.is_file() and entry.suffix == ".json":
                paths.append(entry)
    total = sum(p.stat().st_size for p in paths if p.exists())
    return CleanTarget(
        name="diagnostics",
        label="Scraper diagnostic snapshots",
        description=f"ScraperDiagnostics/*.json ({len(paths)} files)",
        size_bytes=total, item_count=len(paths), item_label="files",
        paths=paths, freshness_detail=[],
    )


def _scan_old_listings() -> CleanTarget:
    paths: List[Path] = []
    if FETCHED_ROOT.exists():
        for provider_dir in sorted(FETCHED_ROOT.iterdir()):
            if not provider_dir.is_dir() or provider_dir.name not in KNOWN_PROVIDERS:
                continue
            for zip_dir in sorted(provider_dir.iterdir()):
                if not zip_dir.is_dir():
                    continue
                for pattern in ("listings_*.json", "canonical_listings_*.json"):
                    candidates = sorted(zip_dir.glob(pattern), reverse=True)
                    paths.extend(candidates[1:])
    total = sum(p.stat().st_size for p in paths if p.exists())
    return CleanTarget(
        name="old_listings",
        label="Outdated listing snapshots",
        description=f"Data/Fetched/{{provider}}/{{zip}}/listings_*.json ({len(paths)} older files)",
        size_bytes=total, item_count=len(paths), item_label="files",
        paths=paths, freshness_detail=[],
    )


def _scan_fetched_data(older_than_days: Optional[float] = None) -> CleanTarget:
    """
    ZIP-level listing directories under Data/Fetched/{provider}/.

    When older_than_days is given, only includes ZIP dirs whose newest listing
    file is older than that many days.  Without it, all ZIP dirs are targeted.
    """
    paths: List[Path] = []
    detail: List[Tuple[str, str, float]] = []
    if not FETCHED_ROOT.exists():
        return CleanTarget(
            name="fetched_data", label="Scraped listing data",
            description="Data/Fetched/{provider}/{zip}/ (none found)",
            size_bytes=0, item_count=0, item_label="dirs",
            paths=[], freshness_detail=[],
        )

    for provider_dir in sorted(FETCHED_ROOT.iterdir()):
        if not provider_dir.is_dir() or provider_dir.name not in KNOWN_PROVIDERS:
            continue
        provider = provider_dir.name
        for zip_dir in sorted(provider_dir.iterdir()):
            if not zip_dir.is_dir():
                continue
            zip_code = zip_dir.name
            mtime = _newest_listing_mtime(zip_dir)
            if mtime is None:
                continue  # no listing files, skip
            age = _age_days(mtime)
            if older_than_days is not None and age < older_than_days:
                continue
            paths.append(zip_dir)
            detail.append((provider, zip_code, age))

    total = sum(_dir_size(p) for p in paths)
    freshness_note = (
        f" older than {older_than_days:.0f}d" if older_than_days is not None else ""
    )
    return CleanTarget(
        name="fetched_data",
        label="Scraped listing data",
        description=f"Data/Fetched/{{provider}}/{{zip}}/{{{len(paths)} ZIP dirs{freshness_note}}}",
        size_bytes=total, item_count=len(paths), item_label="dirs",
        paths=paths, freshness_detail=detail,
    )


def _scan_archive() -> CleanTarget:
    paths: List[Path] = []
    size = 0
    if ARCHIVE_ROOT.exists():
        paths = [ARCHIVE_ROOT]
        size = _dir_size(ARCHIVE_ROOT)
    return CleanTarget(
        name="archive",
        label="Legacy property reset archive",
        description="Data/_property_reset_archive/",
        size_bytes=size, item_count=len(paths), item_label="dirs",
        paths=paths, freshness_detail=[],
    )


def scan(
    targets: Optional[List[str]] = None,
    older_than_days: Optional[float] = None,
) -> Dict[str, CleanTarget]:
    selected = targets if targets else ALL_TARGETS
    result = {}
    for name in selected:
        if name == "fetched_data":
            result[name] = _scan_fetched_data(older_than_days)
        elif name == "chrome_profiles":
            result[name] = _scan_chrome_profiles()
        elif name == "diagnostics":
            result[name] = _scan_diagnostics()
        elif name == "old_listings":
            result[name] = _scan_old_listings()
        elif name == "archive":
            result[name] = _scan_archive()
    return result


# ── Deletion ──────────────────────────────────────────────────────────────────

def _delete(target: CleanTarget) -> Tuple[int, List[str]]:
    deleted = 0
    errors: List[str] = []
    for p in target.paths:
        try:
            if p.is_dir():
                shutil.rmtree(p)
            elif p.is_file():
                p.unlink()
            deleted += 1
        except Exception as exc:
            errors.append(f"{p}: {exc}")
    return deleted, errors


def execute_cleanup(results: Dict[str, CleanTarget]) -> Dict[str, dict]:
    summary: Dict[str, dict] = {}
    for name, target in results.items():
        deleted, errors = _delete(target)
        summary[name] = {
            "deleted": deleted,
            "errors": errors,
            "bytes_freed": target.size_bytes if not errors else None,
        }
    return summary


# ── Formatting ────────────────────────────────────────────────────────────────

def format_text_report(
    results: Dict[str, CleanTarget],
    *,
    dry_run: bool = True,
    delete_summary: Optional[Dict[str, dict]] = None,
    older_than_days: Optional[float] = None,
) -> str:
    lines = []
    lines.append("=" * 68)
    mode = "Dry Run — nothing deleted" if dry_run else "Cleanup Executed"
    lines.append(f"  Data Cleanup  [{mode}]")
    lines.append("=" * 68)
    lines.append("")

    total_bytes = sum(t.size_bytes for t in results.values())
    total_items = sum(t.item_count for t in results.values())

    col_w = 36
    lines.append(f"  {'Target':<{col_w}} {'Size':>9}   {'Items':>6}")
    lines.append("  " + "─" * 64)
    for name, target in results.items():
        if delete_summary and name in delete_summary:
            ds = delete_summary[name]
            status = f"  ✓ deleted {ds['deleted']}"
            if ds.get("errors"):
                status += f"  ({len(ds['errors'])} errors)"
        else:
            status = ""
        size_str = _fmt_size(target.size_bytes) if target.size_bytes else "  0 B"
        item_str = f"{target.item_count} {target.item_label}"
        lines.append(
            f"  {target.label:<{col_w}} {size_str:>9}   {item_str:>10}{status}"
        )
        lines.append(f"    {target.description}")

        # Show freshness breakdown for fetched_data
        if name == "fetched_data" and target.freshness_detail and dry_run and not delete_summary:
            ages = [age for _, _, age in target.freshness_detail]
            if ages:
                thresholds = [7, 14, 30]
                lines.append(
                    "    Age breakdown: " +
                    "  ".join(
                        f">{t}d: {sum(1 for a in ages if a > t)}"
                        for t in thresholds
                    ) +
                    f"  max: {max(ages):.0f}d"
                )
            if len(target.freshness_detail) <= 20:
                lines.append(f"    {'Provider':<10} {'ZIP':<8} {'Age':>6}")
                for provider, zip_code, age in sorted(
                    target.freshness_detail, key=lambda x: -x[2]
                ):
                    lines.append(f"      {provider:<10} {zip_code:<8} {age:>4.0f}d")

    lines.append("")
    lines.append("  " + "─" * 64)
    if delete_summary:
        freed = sum(
            ds.get("bytes_freed") or 0
            for ds in delete_summary.values()
            if ds.get("bytes_freed") is not None
        )
        lines.append(f"  {'Freed:':<{col_w}} {_fmt_size(freed):>9}")
    else:
        lines.append(
            f"  {'Total reclaimable:':<{col_w}} {_fmt_size(total_bytes):>9}   {total_items} items"
        )
    lines.append("")

    if dry_run and not delete_summary:
        lines.append("  Run with --execute to delete the above.")
        lines.append("  Use --target to limit which categories are cleaned.")
        if "fetched_data" not in (results or {}):
            lines.append("  Use --target fetched_data --older-than-days N to clear stale scraped data.")

    lines.append("=" * 68)
    lines.append("")
    return "\n".join(lines)


def format_json_report(
    results: Dict[str, CleanTarget],
    *,
    dry_run: bool = True,
    delete_summary: Optional[Dict[str, dict]] = None,
) -> str:
    out: dict = {
        "dry_run": dry_run,
        "total_bytes": sum(t.size_bytes for t in results.values()),
        "total_items": sum(t.item_count for t in results.values()),
        "targets": {},
    }
    for name, target in results.items():
        entry: dict = {
            "label": target.label,
            "size_bytes": target.size_bytes,
            "item_count": target.item_count,
            "item_label": target.item_label,
        }
        if target.freshness_detail:
            entry["freshness_detail"] = [
                {"provider": p, "zip_code": z, "age_days": round(a, 1)}
                for p, z, a in target.freshness_detail
            ]
        if delete_summary and name in delete_summary:
            entry["delete_result"] = delete_summary[name]
        out["targets"][name] = entry
    return json.dumps(out, indent=2)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Show and optionally delete temporary/stale scraper data."
    )
    parser.add_argument(
        "--target", nargs="+", metavar="NAME",
        choices=ALL_TARGETS,
        help=(
            "Which categories to include. "
            f"Choices: {', '.join(ALL_TARGETS)}. Default: all."
        ),
    )
    parser.add_argument(
        "--older-than-days", type=float, default=None, metavar="N",
        help=(
            "For fetched_data: only target ZIP dirs whose newest listing file "
            "is older than N days. Applies to chrome_profiles and diagnostics too."
        ),
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually delete the selected items. Default is dry-run (show only).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output machine-readable JSON.",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt when --execute is given.",
    )
    args = parser.parse_args(argv)

    results = scan(args.target, older_than_days=args.older_than_days)

    if not args.json:
        print(format_text_report(
            results, dry_run=not args.execute, older_than_days=args.older_than_days
        ))

    delete_summary: Optional[Dict[str, dict]] = None

    if args.execute:
        total_bytes = sum(t.size_bytes for t in results.values())
        total_items = sum(t.item_count for t in results.values())
        if total_items == 0:
            if not args.json:
                print("Nothing to delete.")
            else:
                print(format_json_report(results, dry_run=False, delete_summary={}))
            return

        if not args.yes and not args.json:
            prompt = (
                f"Delete {total_items} items ({_fmt_size(total_bytes)})? "
                "[yes/N] "
            )
            answer = input(prompt).strip().lower()
            if answer not in ("yes", "y"):
                print("Aborted.")
                return

        delete_summary = execute_cleanup(results)

        if args.json:
            print(format_json_report(results, dry_run=False, delete_summary=delete_summary))
        else:
            print(format_text_report(
                results, dry_run=False, delete_summary=delete_summary,
                older_than_days=args.older_than_days,
            ))
    elif args.json:
        print(format_json_report(results, dry_run=True))


if __name__ == "__main__":
    main()
