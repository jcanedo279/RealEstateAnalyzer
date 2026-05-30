from datetime import datetime
import json
from pathlib import Path
import re

from re_analyzer.utility.utility import DATA_PATH, ensure_directory_exists, load_json, save_json


RANKING_FIELDS = (
    "address",
    "source",
    "status_verified",
    "price",
    "rent_estimate",
    "rent_source",
    "rent_confidence",
    "down_payment_pct",
    "closing_cost_pct",
    "cash_in",
    "principal_and_interest",
    "tax_current_annual",
    "tax_reassessed_annual",
    "insurance_monthly",
    "hoa_monthly",
    "vacancy_pct",
    "management_pct",
    "repairs_pct",
    "capex_pct",
    "noi_current_tax",
    "noi_reassessed_tax",
    "cash_flow_current_tax",
    "cash_flow_reassessed_tax",
    "cash_on_cash_current_tax",
    "cash_on_cash_reassessed_tax",
    "cap_rate_current_tax",
    "cap_rate_reassessed_tax",
    "dscr_current_tax",
    "dscr_reassessed_tax",
    "break_even_rent_current_tax",
    "break_even_rent_reassessed_tax",
    "price_per_sqft",
    "rent_per_sqft",
    "beds",
    "baths",
    "year_built",
    "condition_flags",
    "data_quality_score",
)


def manual_analysis_dir():
    return Path(DATA_PATH) / "ManualAnalysis"


def _slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return slug or "manual_property_analysis"


def save_manual_analysis_report(report, slug=None):
    output_dir = manual_analysis_dir()
    ensure_directory_exists(output_dir)
    payload = dict(report)
    payload.setdefault("generated_at", datetime.now().isoformat())
    output_path = output_dir / f"{_slugify(slug or payload.get('address'))}.json"
    save_json(payload, output_path)
    return str(output_path)


def load_manual_analysis_reports():
    output_dir = manual_analysis_dir()
    if not output_dir.exists():
        return []
    reports = []
    for path in sorted(output_dir.glob("*.json")):
        report = load_json(path)
        if isinstance(report, dict):
            report["_path"] = str(path)
            reports.append(report)
    return reports


def manual_analysis_to_ranking_row(report):
    assumptions = report.get("input_assumptions") or {}
    metrics = report.get("investment_metrics") or {}
    scenarios = report.get("scenarios") or {}
    property_facts = report.get("property_facts") or {}
    reserves = assumptions.get("reserve_load") or {}
    return {
        "address": report.get("address"),
        "source": "manual_analysis",
        "status_verified": report.get("status_verification", {}).get("verified"),
        "price": assumptions.get("price"),
        "rent_estimate": assumptions.get("monthly_rent"),
        "rent_source": assumptions.get("rent_source"),
        "rent_confidence": assumptions.get("rent_confidence"),
        "down_payment_pct": assumptions.get("down_payment_pct"),
        "closing_cost_pct": assumptions.get("closing_cost_pct"),
        "cash_in": assumptions.get("cash_in"),
        "principal_and_interest": assumptions.get("principal_and_interest_monthly"),
        "tax_current_annual": assumptions.get("tax_current_annual"),
        "tax_reassessed_annual": assumptions.get("tax_reassessed_annual"),
        "insurance_monthly": assumptions.get("insurance_monthly"),
        "hoa_monthly": assumptions.get("hoa_monthly"),
        "vacancy_pct": reserves.get("vacancy_pct"),
        "management_pct": reserves.get("management_pct"),
        "repairs_pct": reserves.get("repairs_pct"),
        "capex_pct": reserves.get("capex_pct"),
        "noi_current_tax": metrics.get("noi_current_tax"),
        "noi_reassessed_tax": metrics.get("noi_reassessed_tax"),
        "cash_flow_current_tax": scenarios.get("with_reserves_current_tax", {}).get("annual_cash_flow"),
        "cash_flow_reassessed_tax": scenarios.get("with_reserves_reassessed_tax", {}).get("annual_cash_flow"),
        "cash_on_cash_current_tax": scenarios.get("with_reserves_current_tax", {}).get("cash_on_cash"),
        "cash_on_cash_reassessed_tax": scenarios.get("with_reserves_reassessed_tax", {}).get("cash_on_cash"),
        "cap_rate_current_tax": metrics.get("cap_rate_current_tax"),
        "cap_rate_reassessed_tax": metrics.get("cap_rate_reassessed_tax"),
        "dscr_current_tax": metrics.get("dscr_current_tax"),
        "dscr_reassessed_tax": metrics.get("dscr_reassessed_tax"),
        "break_even_rent_current_tax": metrics.get("break_even_rent_current_tax"),
        "break_even_rent_reassessed_tax": metrics.get("break_even_rent_reassessed_tax"),
        "price_per_sqft": metrics.get("price_per_sqft"),
        "rent_per_sqft": metrics.get("rent_per_sqft"),
        "beds": property_facts.get("beds"),
        "baths": property_facts.get("baths_modeled"),
        "year_built": property_facts.get("year_built"),
        "condition_flags": report.get("condition_flags"),
        "data_quality_score": report.get("data_quality_score"),
    }


def load_manual_analysis_ranking_rows():
    return [manual_analysis_to_ranking_row(report) for report in load_manual_analysis_reports()]


def main():
    print(json.dumps(load_manual_analysis_ranking_rows(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
