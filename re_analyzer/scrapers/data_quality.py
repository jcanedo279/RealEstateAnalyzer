"""
Report field-coverage quality across all locally stored canonical listing JSON files.

Usage
-----
    python -m re_analyzer.scrapers.data_quality
    python -m re_analyzer.scrapers.data_quality --provider zillow redfin
    python -m re_analyzer.scrapers.data_quality --zip 32011 32034
    python -m re_analyzer.scrapers.data_quality --resource         # add disk/freshness section
    python -m re_analyzer.scrapers.data_quality --resource --per-zip
    python -m re_analyzer.scrapers.data_quality --json
    python -m re_analyzer.scrapers.data_quality --output report.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from re_analyzer.utility.utility import DATA_PATH

FETCHED_ROOT = Path(DATA_PATH) / "Fetched"
KNOWN_PROVIDERS = ["zillow", "redfin", "realtor"]

# ── Field groups ─────────────────────────────────────────────────────────────

_CORE_FIELDS: List[Tuple[str, str]] = [
    ("price",        "list price"),
    ("beds",         "bedrooms"),
    ("baths",        "bathrooms"),
    ("living_area",  "sq ft"),
    ("lot_size",     "lot sq ft"),
    ("year_built",   "year built"),
    ("latitude",     "latitude"),
    ("longitude",    "longitude"),
    ("home_type",    "home type"),
    ("url",          "listing URL"),
]

_ESTIMATE_FIELDS: List[Tuple[str, str]] = [
    ("price_estimate",  "any price estimate"),
    ("rent_estimate",   "any rent estimate"),
]

_AVM_SOURCES: List[Tuple[str, str, str]] = [
    ("price_estimates", "zillow_zestimate",       "zillow zestimate"),
    ("price_estimates", "quantarium",             "quantarium AVM"),
    ("price_estimates", "cotality",               "cotality (CoreLogic) AVM"),
    ("price_estimates", "collateral_analytics",   "collateral analytics AVM"),
    ("price_estimates", "redfin_avm",             "redfin AVM"),
    ("rent_estimates",  "zillow_rent_zestimate",  "zillow rent zestimate"),
    ("rent_estimates",  "redfin_rental_estimate", "redfin rental estimate"),
]

_HISTORY_FIELDS: List[Tuple[str, str]] = [
    ("price_history",    "price history"),
    ("tax_history",      "tax history"),
    ("estimate_history", "estimate history"),
]

# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_present(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict)):
        return len(value) > 0
    if isinstance(value, str):
        return bool(value.strip())
    return True


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


def _age_days(mtime: float) -> float:
    return (time.time() - mtime) / 86400


def _latest_canonical_files(
    provider_filter: Optional[List[str]],
    zip_filter: Optional[List[str]],
):
    """Yield (provider, zip_code, path) for the most recent canonical JSON per provider/zip."""
    if not FETCHED_ROOT.exists():
        return
    for provider_dir in sorted(FETCHED_ROOT.iterdir()):
        if not provider_dir.is_dir():
            continue
        provider = provider_dir.name
        if provider not in KNOWN_PROVIDERS:
            continue
        if provider_filter and provider not in provider_filter:
            continue
        for zip_dir in sorted(provider_dir.iterdir()):
            if not zip_dir.is_dir():
                continue
            zip_code = zip_dir.name
            if zip_filter and zip_code not in zip_filter:
                continue
            candidates = sorted(zip_dir.glob("canonical_listings_*.json"), reverse=True)
            if candidates:
                yield provider, zip_code, candidates[0]


def _load_listings(path: Path) -> List[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ── Quality analysis ─────────────────────────────────────────────────────────

class ProviderStats:
    def __init__(self, provider: str):
        self.provider = provider
        self.zip_count = 0
        self.total = 0
        self.field_counts: Dict[str, int] = defaultdict(int)

    def add_listing(self, listing: dict):
        self.total += 1
        for field, _ in _CORE_FIELDS:
            if _is_present(listing.get(field)):
                self.field_counts[field] += 1
        for field, _ in _ESTIMATE_FIELDS:
            if _is_present(listing.get(field)):
                self.field_counts[field] += 1
        for dict_key, sub_key, _ in _AVM_SOURCES:
            d = listing.get(dict_key)
            if isinstance(d, dict) and _is_present(d.get(sub_key)):
                self.field_counts[f"{dict_key}.{sub_key}"] += 1
        for field, _ in _HISTORY_FIELDS:
            if _is_present(listing.get(field)):
                self.field_counts[field] += 1

    def pct(self, key: str) -> float:
        if not self.total:
            return 0.0
        return 100.0 * self.field_counts[key] / self.total


def analyze(
    provider_filter: Optional[List[str]] = None,
    zip_filter: Optional[List[str]] = None,
) -> Dict[str, ProviderStats]:
    stats: Dict[str, ProviderStats] = {}
    for provider, zip_code, path in _latest_canonical_files(provider_filter, zip_filter):
        if provider not in stats:
            stats[provider] = ProviderStats(provider)
        stats[provider].zip_count += 1
        for listing in _load_listings(path):
            stats[provider].add_listing(listing)
    return stats


# ── Resource utilization analysis ────────────────────────────────────────────

class ZipResourceInfo:
    __slots__ = ("provider", "zip_code", "listing_count",
                 "raw_bytes", "canonical_bytes", "age_days")

    def __init__(
        self,
        provider: str,
        zip_code: str,
        listing_count: int,
        raw_bytes: int,
        canonical_bytes: int,
        age_days: float,
    ):
        self.provider = provider
        self.zip_code = zip_code
        self.listing_count = listing_count
        self.raw_bytes = raw_bytes
        self.canonical_bytes = canonical_bytes
        self.age_days = age_days

    @property
    def total_bytes(self) -> int:
        return self.raw_bytes + self.canonical_bytes


def analyze_resources(
    provider_filter: Optional[List[str]] = None,
    zip_filter: Optional[List[str]] = None,
) -> List[ZipResourceInfo]:
    rows: List[ZipResourceInfo] = []
    if not FETCHED_ROOT.exists():
        return rows
    for provider_dir in sorted(FETCHED_ROOT.iterdir()):
        if not provider_dir.is_dir() or provider_dir.name not in KNOWN_PROVIDERS:
            continue
        provider = provider_dir.name
        if provider_filter and provider not in provider_filter:
            continue
        for zip_dir in sorted(provider_dir.iterdir()):
            if not zip_dir.is_dir():
                continue
            zip_code = zip_dir.name
            if zip_filter and zip_code not in zip_filter:
                continue

            canonical_files = sorted(zip_dir.glob("canonical_listings_*.json"), reverse=True)
            raw_files = sorted(zip_dir.glob("listings_*.json"), reverse=True)
            if not canonical_files and not raw_files:
                continue

            # Listing count from newest canonical file
            listing_count = 0
            newest_mtime = 0.0
            if canonical_files:
                newest_mtime = canonical_files[0].stat().st_mtime
                listings = _load_listings(canonical_files[0])
                listing_count = len(listings)

            # Sizes (all versions, not just newest)
            raw_bytes = sum(
                f.stat().st_size for f in zip_dir.glob("listings_*.json")
                if f.is_file()
            )
            canonical_bytes = sum(
                f.stat().st_size for f in zip_dir.glob("canonical_listings_*.json")
                if f.is_file()
            )

            # Age from newest canonical file mtime
            age = _age_days(newest_mtime) if newest_mtime else 0.0

            rows.append(ZipResourceInfo(
                provider=provider,
                zip_code=zip_code,
                listing_count=listing_count,
                raw_bytes=raw_bytes,
                canonical_bytes=canonical_bytes,
                age_days=age,
            ))
    return rows


# ── Text formatting ───────────────────────────────────────────────────────────

def _pct_str(pct: float) -> str:
    return f"{pct:5.1f}%"


def _merged_stats(all_stats: Dict[str, ProviderStats]) -> ProviderStats:
    merged = ProviderStats("all")
    for ps in all_stats.values():
        merged.zip_count += ps.zip_count
        merged.total += ps.total
        for k, v in ps.field_counts.items():
            merged.field_counts[k] += v
    return merged


def format_text_report(
    all_stats: Dict[str, ProviderStats],
    resource_rows: Optional[List[ZipResourceInfo]] = None,
    *,
    per_zip: bool = False,
) -> str:
    if not all_stats:
        return "No canonical listing data found.\n"

    providers = sorted(all_stats.keys())
    merged = _merged_stats(all_stats)

    lines: List[str] = []
    lines.append("=" * 72)
    lines.append("  Data Quality Report")
    lines.append("=" * 72)

    lines.append("")
    lines.append(f"  Providers:  {', '.join(providers)}")
    lines.append(f"  ZIPs:       {merged.zip_count}  " +
                 "  ".join(f"{p}: {all_stats[p].zip_count}" for p in providers))
    lines.append(f"  Listings:   {merged.total:,}  " +
                 "  ".join(f"{p}: {all_stats[p].total:,}" for p in providers))

    header = f"  {'Field':<34}" + f"{'All':>7}" + "".join(f"{p:>10}" for p in providers)
    divider = "  " + "─" * 70

    def row(label: str, key: str) -> str:
        p_parts = "".join(_pct_str(all_stats[p].pct(key)) for p in providers)
        return f"  {label:<34}{_pct_str(merged.pct(key))}{p_parts}"

    def section(title: str, rows: list) -> list:
        out = ["", f"  ── {title} " + "─" * max(0, 68 - len(title) - 4), header, divider]
        out.extend(rows)
        return out

    lines.extend(section("Core Property Fields",
                          [row(label, field) for field, label in _CORE_FIELDS]))
    lines.extend(section("Estimate Coverage",
                          [row(label, field) for field, label in _ESTIMATE_FIELDS]))
    lines.extend(section("AVM Sources",
                          [row(label, f"{dk}.{sk}") for dk, sk, label in _AVM_SOURCES]))
    lines.extend(section("History Fields",
                          [row(label, field) for field, label in _HISTORY_FIELDS]))

    # ── Resource utilization ─────────────────────────────────────────────────
    if resource_rows is not None:
        lines.extend(_format_resource_section(resource_rows, providers, per_zip=per_zip))

    lines.append("")
    lines.append("=" * 72)
    lines.append("")
    return "\n".join(lines)


def _format_resource_section(
    rows: List[ZipResourceInfo],
    providers: List[str],
    *,
    per_zip: bool = False,
) -> List[str]:
    lines: List[str] = []
    lines.append("")
    lines.append("  ── Resource Utilization " + "─" * 48)

    if not rows:
        lines.append("  (no data)")
        return lines

    # Per-provider summary
    provider_groups: Dict[str, List[ZipResourceInfo]] = defaultdict(list)
    for r in rows:
        provider_groups[r.provider].append(r)

    total_listings = sum(r.listing_count for r in rows)
    total_raw = sum(r.raw_bytes for r in rows)
    total_canonical = sum(r.canonical_bytes for r in rows)
    total_bytes = total_raw + total_canonical
    all_ages = [r.age_days for r in rows]

    lines.append(f"  Total disk (Data/Fetched/):  {_fmt_size(total_bytes)}")
    lines.append("")

    h = (f"  {'Provider':<10} {'ZIPs':>5} {'Listings':>9} "
         f"{'Raw':>9} {'Canonical':>10} {'Age min/max':>12}")
    lines.append(h)
    lines.append("  " + "─" * 60)

    for p in sorted(provider_groups.keys()):
        g = provider_groups[p]
        p_listings = sum(r.listing_count for r in g)
        p_raw = sum(r.raw_bytes for r in g)
        p_can = sum(r.canonical_bytes for r in g)
        p_ages = [r.age_days for r in g]
        age_str = f"{min(p_ages):.0f}d / {max(p_ages):.0f}d"
        lines.append(
            f"  {p:<10} {len(g):>5} {p_listings:>9,} "
            f"{_fmt_size(p_raw):>9} {_fmt_size(p_can):>10} {age_str:>12}"
        )

    lines.append("  " + "─" * 60)
    age_str = f"{min(all_ages):.0f}d / {max(all_ages):.0f}d" if all_ages else "—"
    lines.append(
        f"  {'All':<10} {len(rows):>5} {total_listings:>9,} "
        f"{_fmt_size(total_raw):>9} {_fmt_size(total_canonical):>10} {age_str:>12}"
    )

    # Freshness breakdown
    lines.append("")
    thresholds = [1, 7, 14, 30]
    lines.append("  Freshness (ZIPs with data older than threshold):")
    parts = []
    for t in thresholds:
        count = sum(1 for r in rows if r.age_days > t)
        parts.append(f">{t}d: {count}")
    lines.append("    " + "   ".join(parts))

    # Per-ZIP table
    if per_zip:
        lines.append("")
        lines.append(
            f"  {'ZIP':<8} {'Provider':<10} {'Listings':>9} "
            f"{'Size':>9} {'Age':>6}"
        )
        lines.append("  " + "─" * 50)
        for r in sorted(rows, key=lambda x: (x.provider, x.zip_code)):
            age_str = f"{r.age_days:.0f}d"
            lines.append(
                f"  {r.zip_code:<8} {r.provider:<10} {r.listing_count:>9,} "
                f"{_fmt_size(r.total_bytes):>9} {age_str:>6}"
            )

    return lines


def format_json_report(
    all_stats: Dict[str, ProviderStats],
    resource_rows: Optional[List[ZipResourceInfo]] = None,
) -> str:
    providers = sorted(all_stats.keys())
    merged = _merged_stats(all_stats)

    def provider_dict(ps: ProviderStats) -> dict:
        fields: dict = {}
        for field, _ in _CORE_FIELDS:
            fields[field] = {"count": ps.field_counts[field], "pct": round(ps.pct(field), 2)}
        for field, _ in _ESTIMATE_FIELDS:
            fields[field] = {"count": ps.field_counts[field], "pct": round(ps.pct(field), 2)}
        for dk, sk, _ in _AVM_SOURCES:
            key = f"{dk}.{sk}"
            fields[key] = {"count": ps.field_counts[key], "pct": round(ps.pct(key), 2)}
        for field, _ in _HISTORY_FIELDS:
            fields[field] = {"count": ps.field_counts[field], "pct": round(ps.pct(field), 2)}
        return {"zip_count": ps.zip_count, "total_listings": ps.total, "fields": fields}

    report: dict = {"quality": {"all": provider_dict(merged)}}
    for p in providers:
        report["quality"][p] = provider_dict(all_stats[p])

    if resource_rows is not None:
        provider_groups: Dict[str, List[ZipResourceInfo]] = defaultdict(list)
        for r in resource_rows:
            provider_groups[r.provider].append(r)

        resource: dict = {
            "total_bytes": sum(r.total_bytes for r in resource_rows),
            "total_listings": sum(r.listing_count for r in resource_rows),
            "providers": {},
        }
        for p, g in sorted(provider_groups.items()):
            resource["providers"][p] = {
                "zip_count": len(g),
                "total_listings": sum(r.listing_count for r in g),
                "raw_bytes": sum(r.raw_bytes for r in g),
                "canonical_bytes": sum(r.canonical_bytes for r in g),
                "min_age_days": round(min(r.age_days for r in g), 1),
                "max_age_days": round(max(r.age_days for r in g), 1),
            }
        resource["zip_detail"] = [
            {
                "provider": r.provider,
                "zip_code": r.zip_code,
                "listing_count": r.listing_count,
                "raw_bytes": r.raw_bytes,
                "canonical_bytes": r.canonical_bytes,
                "age_days": round(r.age_days, 1),
            }
            for r in sorted(resource_rows, key=lambda x: (x.provider, x.zip_code))
        ]
        report["resource"] = resource

    return json.dumps(report, indent=2)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report field-coverage quality and resource utilization of local listing data."
    )
    parser.add_argument(
        "--provider", nargs="+", metavar="NAME",
        help="Limit to specific providers (e.g. zillow redfin realtor)."
    )
    parser.add_argument(
        "--zip", nargs="+", metavar="ZIP",
        help="Limit to specific ZIP codes."
    )
    parser.add_argument(
        "--resource", action="store_true",
        help="Include disk usage and data freshness breakdown."
    )
    parser.add_argument(
        "--per-zip", action="store_true",
        help="With --resource: show a row per ZIP code."
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output machine-readable JSON."
    )
    parser.add_argument(
        "--output", metavar="FILE",
        help="Write report to FILE instead of stdout."
    )
    args = parser.parse_args(argv)

    provider_filter = [p.lower() for p in args.provider] if args.provider else None
    zip_filter = [str(z).zfill(5) for z in args.zip] if args.zip else None

    all_stats = analyze(provider_filter, zip_filter)
    resource_rows = analyze_resources(provider_filter, zip_filter) if args.resource else None

    if args.json:
        text = format_json_report(all_stats, resource_rows)
    else:
        text = format_text_report(all_stats, resource_rows, per_zip=args.per_zip)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
