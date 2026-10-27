"""Read-only audit of customer mapping provenance and local affected vouchers.

Run from second_step: python scripts/audit_customer_mappings.py
The report contains review candidates, not a claim that every differing name is wrong.
No Feishu, ERP, Yonyou or workbook data is modified.
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import domestic_voucher_service as domestic
from app.services import domestic_voucher_comparison as comparison
from app.services import voucher_service as voucher
from app.services import workbench_service as workbench


def source_records(path):
    return [dict(item.get("fields", item), record_id=item.get("record_id") or item.get("id"))
            for item in voucher.read_json(path).get("records", [])]


def source_name(record):
    fields = (("客户名称_脚本用", "买方英文名")
              if voucher.to_text(record.get("销售方式")) == "内销"
              else ("买方英文名", "客户名称_脚本用"))
    return next((voucher.to_text(record.get(field)) for field in fields
                 if voucher.to_text(record.get(field))), "")


def audit(source_path, database):
    masters = voucher.MasterData()
    archive = {str(row["code"]): row for row in masters.customers}
    aliases = voucher.read_json(voucher.CUSTOMER_ALIAS_PATH)["aliases"]
    history = voucher.read_json(voucher.CUSTOMER_HISTORY_PATH)["records"]
    records = source_records(source_path)
    def resolve_group(rows, kind):
        if kind == "内销":
            return domestic.customer_for_records(domestic.enrich_buyer_names(rows), masters)
        return masters.customer_for_records(rows)
    by_name = collections.defaultdict(list)
    for record in records:
        by_name[voucher.normalize_name(source_name(record))].append(record)

    def evidence(row):
        affected = by_name[voucher.normalize_name(row["alias"])]
        code = str(row["platform_code"])
        return {
            "source_name": row["alias"], "mapped_code": code,
            "mapped_name": archive.get(code, {}).get("name"),
            "basis": row.get("basis"), "methods": row.get("methods", []),
            "current_resolved_code": masters.customer_code([row["alias"]]),
            "source_records": len(affected),
            "contracts": sorted({voucher.to_text(r.get("合同号_1")) for r in affected}),
            "business_methods": sorted({voucher.to_text(r.get("销售方式")) for r in affected}),
        }

    auto = [evidence(row) for row in aliases
            if row.get("basis") == voucher.AUTO_CUSTOMER_BASIS and row.get("enabled", True)]
    auto_review = [row for row in auto if row["mapped_code"] != row["current_resolved_code"]]
    history_review = [evidence(row) for row in history
                      if voucher.normalize_name(row["alias"]) != voucher.normalize_name(
                          archive.get(str(row["platform_code"]), {}).get("name"))]
    shortnames = collections.defaultdict(list)
    for row in masters.customers:
        if row.get("shortname"):
            shortnames[str(row["shortname"])].append({"code": row["code"], "name": row["name"]})
    collisions = {name: rows for name, rows in shortnames.items()
                  if len({voucher.normalize_name(r["name"]) for r in rows}) > 1}

    fit_evidence = []
    samples_path = ROOT / "data/domestic_voucher_samples_2026.json"
    fit_source_path = ROOT / "data/domestic_sales_2026.json"
    if samples_path.exists() and fit_source_path.exists():
        fit_source = source_records(fit_source_path)
        for sample in voucher.read_json(samples_path).get("samples", []):
            customers = {comparison.aux_value(line, "客户").get("code")
                         for line in sample.get("分录", []) if comparison.aux_value(line, "客户")}
            if len(customers) != 1:
                continue
            code = next(iter(customers))
            contracts = sample.get("匹配合同号_1", [])
            names = sorted({source_name(r) for r in fit_source
                            if voucher.to_text(r.get("合同号_1")) in contracts and source_name(r)})
            for name in names:
                if masters.customer_code([name]) != code:
                    fit_evidence.append({"period": sample.get("期间"),
                        "voucher": sample.get("凭证号"), "voucher_id": sample.get("凭证ID"),
                        "matched_contracts": contracts, "source_name": name,
                        "inferred_code": code, "inferred_name": archive.get(code, {}).get("name")})

    runtime = []
    counts = {}
    if database.exists():
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            counts = dict(db.execute("SELECT kind, count(*) FROM objects GROUP BY kind"))
            for kind, key, body in db.execute("SELECT kind,id,body FROM objects WHERE kind IN ('draft','export')"):
                value = json.loads(body)
                drafts = [value] if kind == "draft" else value.get("drafts", [])
                for draft in drafts:
                    effective = [dict(r, **draft.get("overrides", {}).get(r["record_id"], {}))
                                 for r in draft.get("effective_source") or draft.get("source", [])]
                    if draft.get("business_type") == "内销":
                        effective = domestic.enrich_buyer_names(effective)
                    expected = resolve_group(effective, draft.get("business_type"))
                    codes = sorted({row["客户编码"] for row in draft.get("rows", []) if row.get("客户编码")})
                    mismatch = any(row.get("客户编码") and row["客户编码"] != expected
                                   and row["id"] + "/客户编码" not in draft.get("manual_fields", [])
                                   for row in draft.get("rows", []))
                    if mismatch:
                        runtime.append({"kind": kind, "id": key, "draft_id": draft.get("id"),
                            "contracts": draft.get("contracts"), "customer_codes": codes,
                            "current_expected_code": expected, "filename": value.get("filename"),
                            "source_names": sorted({source_name(r) for r in effective if source_name(r)})})

    groups = collections.defaultdict(list)
    for record in records:
        if workbench.in_scope(record):
            groups[workbench.group_identity(record)].append(record)
    affected = [row for row in records if voucher.normalize_name(source_name(row)) in {
        voucher.normalize_name(item["source_name"]) for item in auto_review}]
    return {
        "scope": "Local snapshots and local SQLite only; does not establish Yonyou import success or current production state",
        "source_path": str(source_path), "database_path": str(database),
        "summary": {"source_records": len(records), "in_scope_groups": len(groups),
            "automatic_aliases": len(auto), "automatic_aliases_requiring_review": len(auto_review),
            "source_records_using_review_aliases": len(affected),
            "contracts_using_review_aliases": len({voucher.to_text(r.get("合同号_1")) for r in affected}),
            "history_entries": len(history), "history_methods": dict(collections.Counter(
                method for row in history for method in row.get("methods", []))),
            "history_nonidentical_names": len(history_review),
            "shortnames_shared_by_different_names": len(collisions),
            "local_runtime_customer_mismatches": len(runtime)},
        "automatic_aliases_requiring_review": auto_review,
        "history_nonidentical_names_requiring_review": history_review,
        "shortname_collisions_requiring_review": collisions,
        "fitted_cross_customer_evidence": fit_evidence,
        "local_database_counts": counts, "local_runtime_customer_mismatches": runtime,
        "unresolved_source_groups": [{"group": key, "source_names": sorted({source_name(r) for r in rows})}
            for key, rows in groups.items() if not resolve_group(rows, key.split(":", 1)[0])],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/domestic_source_current.json")
    parser.add_argument("--database", type=Path, default=ROOT / "data/workbench.sqlite3")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/customer_mapping_audit.json")
    args = parser.parse_args()
    report = audit(args.source, args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(str(args.output.resolve()))


if __name__ == "__main__":
    main()
