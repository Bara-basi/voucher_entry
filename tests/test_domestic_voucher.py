"""Domestic rule fitting and deterministic voucher generation."""

import json
import copy
import collections
import unittest
from pathlib import Path

from app.services import domestic_voucher_service as domestic
from app.services import voucher_service as legacy

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
        self.assertEqual(accounts["pending_input_tax"], "22210107")
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

    def test_uninvoiced_purchase_uses_pending_tax_account(self):
        records = [
            item for item in self.source["records"]
            if item.get("合同号_1") == "26MT-DP018"
        ]
        rules = copy.deepcopy(self.rules)
        rules["merge_fit"]["content_mode_model"] = []
        voucher = domestic.build_domestic_voucher(
            records, rules, legacy.MasterData()
        )
        pending = [
            row for row in voucher["rows"]
            if row["科目编码"] == "22210107"
        ]
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["摘要"].startswith("暂估-26MT-DP018"))
        self.assertEqual(pending[0]["摘要"], "暂估-26MT-DP018采购商品成本-专票")

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
        self.assertEqual(full["relaxed_matches"], 25)
        self.assertEqual(full["loose_matched_contracts"], 69)
        self.assertEqual(full["loose_mismatched_contracts"], 1)
        self.assertEqual(len(full["not_made_or_not_found_contracts"]), 31)


if __name__ == "__main__":
    unittest.main()
