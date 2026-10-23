"""Fit domestic rules, generate vouchers blindly, then optionally evaluate them."""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import domestic_voucher_service as service
from app.services import voucher_service as legacy

DEFAULT_SOURCE = ROOT / "data/domestic_sales_2026.json"
DEFAULT_SAMPLES = ROOT / "data/domestic_voucher_samples_2026.json"
DEFAULT_RULES = ROOT / "data/domestic_voucher_rules_fit.json"
DEFAULT_MERGE_REPORT = ROOT / "data/domestic_merge_rules_fit.json"
DEFAULT_JSON_OUTPUT = ROOT / "outputs/domestic_vouchers/内销凭证生成结果.json"
DEFAULT_EXCEL_OUTPUT = ROOT / "outputs/domestic_vouchers/内销凭证_批量.xls"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fit(args) -> dict:
    sample_payload = read_json(args.samples)
    source_payload = read_json(args.source)
    voucher_records = read_json(args.voucher_seed).get("records", []) if args.voucher_seed else []
    rules = service.fit_rules(sample_payload, source_payload, voucher_records)
    service.save_json(args.rules, rules)
    service.save_json(args.merge_report, rules["merge_fit"])
    return {
        "rules_file": str(args.rules.resolve()),
        "training_vouchers": rules["training"]["domestic_vouchers"],
        "accounts": rules["accounts"],
        "summary": rules["summary"],
        "mapping_coverage": {
            "customers": len(rules["mappings"]["customers"]),
            "suppliers": len(rules["mappings"]["suppliers"]),
            "inventory_names": len(rules["mappings"]["inventory_names"]),
            "products": len(rules["mappings"]["product_to_inventory"]),
        },
        "merge_report": str(args.merge_report.resolve()),
        "merge_leave_one_out": rules["merge_fit"]["leave_one_out"],
        "content_mode_leave_one_out": rules["merge_fit"]["content_mode_leave_one_out"],
        "content_modes": rules["merge_fit"]["content_modes"],
    }


def generate(args) -> dict:
    source_payload = read_json(args.source)
    rules = read_json(args.rules)
    posting_date = (
        datetime.date.fromisoformat(args.posting_date)
        if args.posting_date else datetime.date.today()
    )
    result = service.generate_all(
        source_payload,
        rules,
        legacy.MasterData(),
    )
    service.write_excel(
        result,
        args.excel,
        posting_date,
        maker=args.maker or rules["training"].get("maker") or "马云凤",
    )
    index_path = service.write_single_excels(
        result,
        args.single_dir,
        posting_date,
        maker=args.single_maker or "王益",
    )
    payload = {
        "posting_date": posting_date.isoformat(),
        "rules_file": str(args.rules.resolve()),
        "source": str(args.source.resolve()),
        **result,
    }
    service.save_json(args.output, payload)
    return {
        "json": str(args.output.resolve()),
        "excel": str(args.excel.resolve()),
        "single_dir": str(args.single_dir.resolve()),
        "single_index": str(index_path.resolve()),
        "generated": result["generated_count"],
        "failed": result["failed_count"],
        "skipped": result["skipped_count"],
        "rows": sum(len(voucher["rows"]) for voucher in result["generated"]),
        "warnings": sum(len(voucher["warnings"]) for voucher in result["generated"]),
    }


def evaluate(args) -> dict:
    generated = read_json(args.output)
    samples = read_json(args.samples)
    voucher_records = read_json(args.voucher_seed).get("records", []) if args.voucher_seed else None
    sales_comparison = service.evaluate(generated, samples)
    full_comparison = service.evaluate_full_lines(generated, samples, voucher_records)
    result = {**sales_comparison, "full_line_comparison": full_comparison}
    report = args.report
    service.save_json(report, result)
    service.write_reconciliation_workbook(result, args.diff_excel)
    return {
        "report": str(report.resolve()),
        "diff_excel": str(args.diff_excel.resolve()),
        "compared_contracts": result["compared_contracts"],
        "exact_revenue_matches": result["exact_revenue_matches"],
        "exact_sales_matches": result["exact_sales_matches"],
        "full_compared_contracts": full_comparison["compared_contracts"],
        "exact_accounting_matches": full_comparison["exact_accounting_matches"],
        "relaxed_matches": full_comparison["relaxed_matches"],
        "exact_account_total_matches": full_comparison["exact_account_total_matches"],
        "exact_ordered_matches": full_comparison["exact_ordered_matches"],
        "exact_full_matches": full_comparison["exact_full_matches"],
        "loose_matched_contracts": full_comparison["loose_matched_contracts"],
        "loose_mismatched_contracts": full_comparison["loose_mismatched_contracts"],
        "not_made_or_not_found_contracts": len(
            full_comparison["not_made_or_not_found_contracts"]
        ),
        "skipped_without_contract_lines": result["skipped_without_contract_lines"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--voucher-seed", type=Path, required=False)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--merge-report", type=Path, default=DEFAULT_MERGE_REPORT)
    parser.add_argument("--posting-date", default=None)
    parser.add_argument("--maker", default=None)
    parser.add_argument(
        "--single-maker",
        default=None,
        help="Maker for individually imported test vouchers (default: 王益)",
    )
    parser.add_argument("--excel", type=Path, default=DEFAULT_EXCEL_OUTPUT)
    parser.add_argument(
        "--single-dir",
        type=Path,
        default=ROOT / "outputs/domestic_vouchers/单张导入模板_王益_无暂估",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "outputs/domestic_vouchers/历史凭证对账报告.json",
    )
    parser.add_argument(
        "--diff-excel",
        type=Path,
        default=ROOT / "outputs/domestic_vouchers/历史凭证逐行差异.xlsx",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("fit")
    subcommands.add_parser("generate")
    subcommands.add_parser("evaluate")
    subcommands.add_parser("all")
    args = parser.parse_args()

    if args.command == "fit":
        result = fit(args)
    elif args.command == "generate":
        result = generate(args)
    elif args.command == "evaluate":
        result = evaluate(args)
    else:
        fit_result = fit(args)
        generated = generate(args)
        evaluated = evaluate(args)
        result = {"fit": fit_result, "generate": generated, "evaluate": evaluated}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
