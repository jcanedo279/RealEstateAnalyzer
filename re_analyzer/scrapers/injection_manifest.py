"""Build the scraper-to-backend injection handoff manifest.

The manifest intentionally points only at canonical/raw JSON payloads. Browser
diagnostics, screenshots, HTML snapshots, and downloaded images stay out of the
database injection path.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from re_analyzer.utility.utility import DATA_PATH
except ImportError:  # pragma: no cover - only used when the package is imported oddly.
    DATA_PATH = str(Path(__file__).resolve().parents[1] / "Data")


FETCHED_ROOT = Path(DATA_PATH) / "Fetched"
KNOWN_PROVIDERS = ("zillow", "redfin", "realtor")
GLOBAL_MANIFEST_NAME = "injection_manifest.json"
ZIP_MANIFEST_NAME = "injection_manifest_latest.json"
SCHEMA_VERSION = 1

ARTIFACT_POLICY = {
    "database_payloads": [
        "canonical listing JSON",
        "provider raw listing JSON",
    ],
    "excluded_from_database": [
        "browser diagnostics",
        "screenshots",
        "HTML snapshots",
        "downloaded image binaries",
        "browser profile/cache files",
    ],
    "image_fields_are_urls_only": True,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_list_count(path: Path) -> Optional[int]:
    try:
        with path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    return len(payload) if isinstance(payload, list) else None


def _payload_file_info(path: Optional[Path], fetched_root: Path, *, include_record_count: bool = True) -> Optional[dict]:
    if not path:
        return None

    path = Path(path)
    info = {
        "path": _relative_path(path, fetched_root),
        "exists": path.exists(),
    }
    if not path.exists():
        return info

    stat = path.stat()
    info.update({
        "bytes": stat.st_size,
        "sha256": _sha256(path),
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    if include_record_count:
        info["record_count"] = _json_list_count(path)
    return info


def _latest_file(zip_dir: Path, pattern: str) -> Optional[Path]:
    files = sorted(zip_dir.glob(pattern), reverse=True)
    return files[0] if files else None


def _provider_metadata_path(fetched_root: Path, provider: str, zip_code: str) -> Path:
    return fetched_root / provider / "Metadata" / f"{zip_code}_metadata.json"


def build_zip_injection_record(
    provider: str,
    zip_code: str,
    canonical_path: Path,
    *,
    raw_path: Optional[Path] = None,
    metadata_path: Optional[Path] = None,
    fetched_root: Path = FETCHED_ROOT,
    generated_at: Optional[str] = None,
) -> dict:
    """Return the backend-ready handoff record for one provider/ZIP scrape."""
    provider = str(provider).strip().lower()
    zip_code = str(zip_code).strip()
    fetched_root = Path(fetched_root)
    canonical_info = _payload_file_info(Path(canonical_path), fetched_root)
    raw_info = _payload_file_info(Path(raw_path), fetched_root) if raw_path else None
    metadata_info = _payload_file_info(
        Path(metadata_path),
        fetched_root,
        include_record_count=False,
    ) if metadata_path else None

    ready = bool(canonical_info and canonical_info.get("exists") and canonical_info.get("record_count") is not None)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "provider": provider,
        "zip_code": zip_code,
        "status": "ready" if ready else "invalid",
        "canonical": canonical_info,
        "raw": raw_info,
        "metadata": metadata_info,
        "backend_injection": {
            "task": "ingest_zip_scraped_json",
            "args": {
                "provider": provider,
                "zip_code": zip_code,
            },
        },
    }


def write_zip_injection_manifest(
    provider: str,
    zip_code: str,
    canonical_path: Path,
    *,
    raw_path: Optional[Path] = None,
    metadata_path: Optional[Path] = None,
    fetched_root: Path = FETCHED_ROOT,
    manifest_path: Optional[Path] = None,
) -> dict:
    fetched_root = Path(fetched_root)
    canonical_path = Path(canonical_path)
    if manifest_path is None:
        manifest_path = canonical_path.parent / ZIP_MANIFEST_NAME

    record = build_zip_injection_record(
        provider,
        zip_code,
        canonical_path,
        raw_path=Path(raw_path) if raw_path else None,
        metadata_path=Path(metadata_path) if metadata_path else None,
        fetched_root=fetched_root,
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": record["generated_at"],
        "scope": "provider_zip",
        "fetched_root": str(fetched_root),
        "artifact_policy": ARTIFACT_POLICY,
        "records": [record],
        "summary": {
            "providers": [record["provider"]],
            "zip_codes": [record["zip_code"]],
            "ready_records": 1 if record["status"] == "ready" else 0,
            "canonical_listing_count": int((record.get("canonical") or {}).get("record_count") or 0),
        },
        "backend_workflow": [
            "ingest_zip_scraped_json",
            "rebuild_canonical_parquet",
            "sync_property_catalog",
        ],
    }
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def build_injection_manifest(fetched_root: Path = FETCHED_ROOT, providers: tuple = KNOWN_PROVIDERS) -> dict:
    fetched_root = Path(fetched_root)
    generated_at = _utc_now()
    records = []

    for provider in providers:
        provider_dir = fetched_root / provider
        if not provider_dir.exists():
            continue
        for zip_dir in sorted(provider_dir.iterdir(), key=lambda item: item.name):
            if not zip_dir.is_dir() or not zip_dir.name.isdigit():
                continue
            canonical_path = _latest_file(zip_dir, "canonical_listings_*.json")
            if not canonical_path:
                continue
            raw_path = _latest_file(zip_dir, "listings_*.json")
            metadata_path = _provider_metadata_path(fetched_root, provider, zip_dir.name)
            records.append(build_zip_injection_record(
                provider,
                zip_dir.name,
                canonical_path,
                raw_path=raw_path,
                metadata_path=metadata_path,
                fetched_root=fetched_root,
                generated_at=generated_at,
            ))

    ready_records = [record for record in records if record.get("status") == "ready"]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "scope": "all_latest_scrapes",
        "fetched_root": str(fetched_root),
        "artifact_policy": ARTIFACT_POLICY,
        "records": records,
        "summary": {
            "providers": sorted({record["provider"] for record in records}),
            "zip_codes": sorted({record["zip_code"] for record in records}),
            "ready_records": len(ready_records),
            "invalid_records": len(records) - len(ready_records),
            "canonical_listing_count": sum(int((record.get("canonical") or {}).get("record_count") or 0) for record in ready_records),
        },
        "backend_workflow": [
            "ingest_all_scraped_json",
            "rebuild_canonical_parquet",
            "sync_property_catalog",
        ],
    }


def write_injection_manifest(
    fetched_root: Path = FETCHED_ROOT,
    *,
    output_path: Optional[Path] = None,
    providers: tuple = KNOWN_PROVIDERS,
    dry_run: bool = False,
) -> dict:
    fetched_root = Path(fetched_root)
    manifest = build_injection_manifest(fetched_root=fetched_root, providers=providers)
    if output_path is None:
        output_path = fetched_root / GLOBAL_MANIFEST_NAME

    if not dry_run:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)

    summary = dict(manifest["summary"])
    summary["zip_code_count"] = len(summary.get("zip_codes") or [])
    summary.pop("zip_codes", None)

    return {
        "schema_version": manifest["schema_version"],
        "generated_at": manifest["generated_at"],
        "scope": manifest["scope"],
        "manifest_path": str(output_path),
        "records": len(manifest["records"]),
        "summary": summary,
        "artifact_policy": manifest["artifact_policy"],
        "backend_workflow": manifest["backend_workflow"],
        "written": not dry_run,
    }
