"""Domestic rule fitting and deterministic voucher generation."""

import json
import copy
import collections
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import domestic_voucher_service as domestic
from app.services import draft_service as engine
from app.services import voucher_service as legacy
from app.services import workbench_service as workbench

ROOT = Path(__file__).resolve().parents[1]


class DomesticVoucherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = json.loads(
            (ROOT / "data/domestic_sales_2026.json").read_text(encoding="utf-8")
        )
        cls.samples = json.loads(
            (ROOT / "data/domestic_voucher_samples_2026.json").read_text(encoding="utf-8")
        )
        voucher_path = max(
            (ROOT / "data/yonyou_refresh").glob("*/vouchers/actual_vouchers.json"),
            key=lambda path: path.stat().st_mtime,
        )
        cls.vouchers = json.loads(voucher_path.read_text(encoding="utf-8"))
        cls.rules = domestic.fit_rules(cls.samples, cls.source, cls.vouchers["records"])
        cls.generated = domestic.generate_all(cls.source, cls.rules, legacy.MasterData())

    def test_rules_are_fitted_from_voucher_evidence(self):
        accounts = self.rules["accounts"]
        self.assertEqual(accounts["revenue"], "50010201")
        self.assertEqual(accounts["output_tax"], "22210102")
        self.assertEqual(accounts["input_tax"], "22210101")
        self.assertEqual(accounts["pending_input_tax"], "22210101")
        self.assertEqual(accounts["cost"], "540102")
        self.assertEqual(self.rules["summary"]["purchase"], "采购商品成本")
        self.assertEqual(self.rules["summary"]["cost"], "结转采购成本")
        self.assertEqual(self.rules["merge_fit"]["sample_contracts"], 70)
        self.assertEqual(self.rules["merge_fit"]["patterns"][0]["pattern"], "SPC")
        self.assertFalse(
            self.rules["merge_fit"]["leave_one_out"]["reliable_for_automatic_generation"]
        )
        self.assertEqual(
            self.rules["purchase_order"],
            ["payable", "inventory", "input_tax"],
        )

    def test_generation_is_balanced_and_complete_for_mapped_customers(self):
        self.assertEqual(self.generated["generated_count"], 101)
        self.assertEqual(self.generated["failed_count"], 0)
        self.assertEqual(self.generated["skipped_count"], 1)
        for voucher in self.generated["generated"]:
            self.assertEqual(voucher["totals"]["debit"], voucher["totals"]["credit"])
        self.assertEqual(
            collections.Counter(
                voucher["合并模式"] for voucher in self.generated["generated"]
            ),
            {"full": 55, "sales_cost": 39, "sales": 7},
        )

    def test_all_purchases_use_normal_input_tax_account(self):
        records = [
            item for item in self.source["records"]
            if item.get("合同号_1") == "26MT-DP018"
        ]
        rules = copy.deepcopy(self.rules)
        rules["merge_fit"]["content_mode_model"] = []
        voucher = domestic.build_domestic_voucher(
            records, rules, legacy.MasterData()
        )
        input_taxes = [
            row for row in voucher["rows"]
            if row["科目编码"] == "22210101"
        ]
        self.assertEqual(len(input_taxes), 1)
        self.assertEqual(input_taxes[0]["摘要"], "26MT-DP018采购商品成本-专票")
        self.assertFalse(any("暂估" in row["摘要"] for row in voucher["rows"]))

    def test_missing_domestic_data_keeps_rows_and_reports_errors(self):
        record = copy.deepcopy(next(
            item for item in self.source["records"]
            if item.get("合同号_1") == "26MT-DP018"
        ))
        record.update({
            "供应商": "",
            "供应商简称": "",
            "采购金额": "",
            "客户名称_脚本用": "",
            "买方英文名": "",
        })
        rules = copy.deepcopy(self.rules)
        rules["merge_fit"]["content_mode_model"] = []
        with patch.object(domestic, "RUI_BEI_BUYER_MAPPING", Path("missing-ruibei.json")):
            voucher = domestic.build_domestic_voucher(
                [record], rules, legacy.MasterData()
            )
        self.assertEqual(len(voucher["rows"]), 8)
        messages = "；".join(item["message"] for item in voucher["issues"])
        self.assertIn("未匹配客户", messages)
        self.assertIn("供应商缺失", messages)
        self.assertIn("缺少采购金额", messages)
        payable = next(row for row in voucher["rows"] if row["id"].endswith(":ap"))
        self.assertEqual(payable["供应商编码"], "")
        self.assertNotEqual(payable["贷方金额"], "")
        self.assertTrue(any(item["row_id"].endswith(":ap") for item in voucher["issues"]))
        draft = {
            "rows": voucher["rows"], "build_issues": voucher["issues"],
            "blocking_issues": [], "manual_fields": [], "manual_exception": False,
            "overrides": {}, "calculated_overrides": {},
            "header": {"posting_date": "2026-10-23", "maker": "王益", "category": "记"},
        }
        engine.validate(draft, workbench.catalog())
        self.assertFalse(draft["valid"])
        messages = "；".join(item["message"] for item in draft["issues"])
        self.assertIn("供应商缺失", messages)
        self.assertIn("缺少采购金额", messages)
        self.assertNotIn("请输入有效数值", messages)
        found = {item["row_id"] for item in draft["issues"] if item["row_id"]}
        self.assertTrue(found <= {row["id"] for row in draft["rows"]})

    def test_domestic_income_and_purchase_amount_mismatches_are_errors(self):
        record = copy.deepcopy(next(
            item for item in self.source["records"]
            if item.get("合同号_1") == "26MT-DP018"
        ))
        record["采购金额"] = str(float(record["采购金额"]) + 10)
        record["销项税额"] = str(float(record["销项税额"]) + 2)
        rules = copy.deepcopy(self.rules)
        rules["merge_fit"]["content_mode_model"] = []
        voucher = domestic.build_domestic_voucher([record], rules, legacy.MasterData())
        messages = "；".join(item["message"] for item in voucher["issues"])
        self.assertIn("收入区金额不平", messages)
        self.assertIn("采购金额与不含税成本及进项税额不一致", messages)

    def test_domestic_required_field_matrix_keeps_draft_and_blocks_export(self):
        base = copy.deepcopy(next(
            item for item in self.source["records"]
            if item.get("合同号_1") == "26MT-DP018"
        ))
        rules = copy.deepcopy(self.rules)
        rules["merge_fit"]["content_mode_model"] = []
        cases = [
            ({"供应商": "", "供应商简称": ""}, "供应商缺失"),
            ({"采购金额": ""}, "缺少采购金额"),
            ({"不含税成本": ""}, "缺少有效采购成本"),
            ({"进项税额": ""}, "缺少有效进项税额"),
            ({"报关金额": ""}, "缺少报关金额"),
            ({"币种": ""}, "币种缺失"),
            ({"报关品名": ""}, "未匹配存货"),
            ({"销售方式": ""}, "销售方式不是内销"),
            ({"客户名称_脚本用": "", "买方英文名": ""}, "未匹配客户"),
        ]
        with patch.object(domestic, "RUI_BEI_BUYER_MAPPING", Path("missing-ruibei.json")):
            for changes, expected in cases:
                with self.subTest(expected=expected):
                    record = {**base, **changes}
                    voucher = domestic.build_domestic_voucher(
                        [record], rules, legacy.MasterData()
                    )
                    self.assertEqual(len(voucher["rows"]), 8)
                    messages = "；".join(item["message"] for item in voucher["issues"])
                    self.assertIn(expected, messages)

    def test_post_generation_evaluation_does_not_feed_generation(self):
        report = domestic.evaluate(self.generated, self.samples)
        full = domestic.evaluate_full_lines(
            self.generated, self.samples, self.vouchers["records"]
        )
        self.assertEqual(report["compared_contracts"], 67)
        self.assertEqual(report["exact_revenue_matches"], 66)
        self.assertEqual(full["compared_contracts"], 70)
        self.assertEqual(full["exact_accounting_matches"], 24)
        self.assertEqual(full["exact_account_total_matches"], 41)
        self.assertEqual(full["relaxed_matches"], 24)
        self.assertEqual(full["loose_matched_contracts"], 69)
        self.assertEqual(full["loose_mismatched_contracts"], 1)
        self.assertEqual(len(full["not_made_or_not_found_contracts"]), 31)


if __name__ == "__main__":
    unittest.main()
