"""Build domestic vouchers from the confirmed template; compare history only afterwards."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services import domestic_voucher_service as service
from app.services import domestic_voucher_comparison as comparison
from app.services import draft_service as engine
from app.services import workbench_service as workbench
from app.services import voucher_service as legacy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/domestic_source_current.json")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/domestic_vouchers/内销凭证生成结果.json")
    parser.add_argument("--excel", type=Path, default=ROOT / "outputs/domestic_vouchers/内销凭证_批量.xls")
    parser.add_argument("--single-dir", type=Path, default=ROOT / "outputs/domestic_vouchers/单张导入模板")
    parser.add_argument("--posting-date", default=dt.datetime.now(workbench.store.TZ).date().isoformat())
    parser.add_argument("--maker", default="王益")
    parser.add_argument("--samples", type=Path, default=ROOT / "data/domestic_voucher_samples_2026.json")
    parser.add_argument("--voucher-seed", type=Path)
    parser.add_argument("--report", type=Path, default=ROOT / "outputs/domestic_vouchers/历史差异参考.json")
    parser.add_argument("command", choices=("generate", "evaluate"))
    args = parser.parse_args()
    if args.command == "generate":
        posting_date = dt.date.fromisoformat(args.posting_date)
        result = service.generate_all(legacy.read_json(args.source))
        catalogs = workbench.catalog()
        for voucher in result["generated"]:
            draft = {"rows": voucher["rows"], "build_issues": voucher["issues"],
                     "header": {"posting_date": args.posting_date, "category": "记", "maker": args.maker},
                     "business_type": "内销", "domestic_template_version": service.TEMPLATE_VERSION}
            engine.validate(draft, catalogs)
            voucher["issues"] = draft["issues"]
        result["exported_count"] = sum(not any(i["severity"] == "error" for i in v["issues"]) for v in result["generated"])
        service.save_json(args.output, result)
        service.write_excel(result, args.excel, posting_date, maker=args.maker)
        service.write_single_excels(result, args.single_dir, posting_date, maker=args.maker)
        print(json.dumps({k: v for k, v in result.items() if k not in ("generated", "failed", "skipped")}, ensure_ascii=False))
    else:
        generated = legacy.read_json(args.output)
        samples = legacy.read_json(args.samples)
        records = legacy.read_json(args.voucher_seed)["records"] if args.voucher_seed else None
        report = {"notice": "历史差异仅供复核，不是正确答案，也不回流生成规则",
                  "sales": comparison.evaluate(generated, samples),
                  "lines": comparison.evaluate_full_lines(generated, samples, records)}
        service.save_json(args.report, report)
        print(str(args.report.resolve()))


if __name__ == "__main__":
    main()
