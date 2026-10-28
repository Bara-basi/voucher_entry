"""The user-confirmed template, not historical vouchers, is the test oracle."""
import copy
import datetime as dt
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import xlrd
from app.services import domestic_voucher_service as domestic
from app.services import domestic_voucher_comparison as comparison
from app.services import draft_service as engine
from app.services import voucher_service as legacy
from app.services import workbench_service as svc
from app.services import ruibei_order_cache as orders


class DomesticVoucherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.masters = legacy.MasterData()
        cls.catalogs = svc.catalog()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mapping = Path(self.temp.name) / "buyers.json"
        setting = patch.object(domestic, "RUI_BEI_BUYER_MAPPING", self.mapping)
        setting.start()
        self.addCleanup(setting.stop)
        self.record = {"record_id": "product-1", "合同号_1": "26MT-TEMPLATE", "销售方式": "内销",
            "客户名称_脚本用": "上海辉懿机械有限公司", "买方英文名": "", "币种": "CNY",
            "报关品名": "不锈钢无缝管", "供应商": self.masters.suppliers[0]["name"],
            "报关金额": "1130", "不含税人民币收入": "1000", "销项税额": "130",
            "不含税成本": "500", "进项税额": "65", "采购金额": "565",
            "报关重量": "10", "开票数量（实际）": "999"}

    def build(self, records=None):
        return domestic.build_domestic_voucher(records or [self.record], self.masters)

    def draft(self, voucher, current=True):
        return {"rows": voucher["rows"], "build_issues": voucher["issues"], "source": [self.record],
                "business_type": "内销", "domestic_template_version": domestic.TEMPLATE_VERSION if current else "",
                "header": {"posting_date": "2026-10-26", "category": "记", "maker": "王益"}}

    def test_exact_eight_line_template_amounts_order_aux_and_weight(self):
        result = self.build()
        self.assertEqual(result["issues"], [])
        rows = result["rows"]
        self.assertEqual([r["科目编码"] for r in rows],
            ["112201", "50010201", "22210102", "140501", "22210101", "220201", "540102", "140501"])
        self.assertEqual([(r["借方金额"], r["贷方金额"]) for r in rows],
            [("1130.00", "0"), ("0", "1000.00"), ("0", "130.00"), ("500.00", "0"),
             ("65.00", "0"), ("0", "565.00"), ("500.00", "0"), ("0", "500.00")])
        self.assertEqual(rows[0]["客户编码"], "010243")
        self.assertTrue(rows[5]["供应商编码"])
        self.assertEqual(rows[3]["存货档案"], rows[7]["存货档案"])
        self.assertEqual(rows[3]["借方数量"], "10")
        self.assertEqual(rows[7]["贷方数量"], "10")
        self.assertEqual(rows[3]["贷方数量"], "")
        self.assertEqual(rows[3]["单价"], "50.00")
        self.assertTrue(rows[4]["摘要"].endswith("采购商品成本-专票"))
        self.assertTrue(engine.validate(self.draft(result), self.catalogs)["valid"])

    def test_multiple_products_keep_five_lines_each_even_same_supplier_inventory(self):
        second = {**self.record, "record_id": "product-2"}
        result = self.build([self.record, second])
        self.assertEqual(len(result["rows"]), 13)
        self.assertEqual(result["rows"][0]["借方金额"], "2260.00")
        self.assertEqual([r["id"].split(":")[-1] for r in result["rows"][3:]],
                         ["stock", "tax", "ap", "cost", "out"] * 2)

    def test_no_balancing_backsolve_even_when_section_errors_cancel(self):
        self.record.update(报关金额="1130.01", 采购金额="565.01")
        result = self.build()
        self.assertEqual(result["totals"]["difference"], "0.00")
        self.assertEqual(result["rows"][0]["借方金额"], "1130.01")
        self.assertEqual(result["rows"][5]["贷方金额"], "565.01")
        messages = str(result["issues"])
        self.assertIn("收入区金额不平", messages)
        self.assertIn("采购金额与不含税成本", messages)
        self.assertFalse(engine.validate(self.draft(result), self.catalogs)["valid"])

    def test_missing_values_remain_empty_and_keep_all_rows(self):
        for field in ("采购金额", "不含税成本", "进项税额", "报关金额", "销项税额", "报关重量", "供应商", "报关品名"):
            with self.subTest(field=field):
                record = {**self.record, field: ""}
                result = self.build([record])
                self.assertEqual(len(result["rows"]), 8)
                self.assertTrue(any(i.get("source_field") == field for i in result["issues"]))
                self.assertFalse(engine.validate(self.draft(result), self.catalogs)["valid"])
        missing_purchase = self.build([{**self.record, "采购金额": ""}])
        self.assertEqual(missing_purchase["rows"][5]["贷方金额"], "")
        missing_weight = self.build([{**self.record, "报关重量": ""}])
        self.assertEqual(missing_weight["rows"][3]["借方数量"], "")

    def test_zero_tax_lines_are_kept_and_foreign_currency_fees_block(self):
        result = self.build([{**self.record, "进项税额": "0", "采购金额": "500", "销项税额": "0", "报关金额": "1000"}])
        self.assertEqual(len(result["rows"]), 8)
        self.assertEqual(result["issues"], [])
        native_zero = self.build([{**self.record, "进项税额": 0, "采购金额": 500, "销项税额": 0, "报关金额": 1000}])
        self.assertEqual(native_zero["issues"], [])
        for changes in ({"币种": "USD"}, {"运费": "1"}, {"保费": "1"}, {"港杂费": "1"}):
            with self.subTest(changes=changes):
                result = self.build([{**self.record, **changes}])
                self.assertEqual(len(result["rows"]), 8)
                self.assertTrue(result["issues"])

    def test_unit_price_uses_unrounded_source_cost(self):
        result = self.build([{**self.record, "不含税成本": "100.004", "进项税额": 13,
                             "采购金额": "113.004", "报关重量": ".001"}])
        self.assertEqual(result["rows"][3]["借方金额"], "100.00")
        self.assertEqual(result["rows"][3]["单价"], "100004.00")

    def test_land_freight_does_not_change_basic_amounts_or_add_notices(self):
        result = self.build([{**self.record, "内陆运费含税": "3490"}])
        self.assertEqual(len(result["rows"]), 8)
        self.assertEqual(result["rows"][0]["借方金额"], "1130.00")
        self.assertEqual(result["issues"], [])
        self.assertTrue(engine.validate(self.draft(result), self.catalogs)["valid"])


    def test_old_drafts_block_and_workbench_does_not_read_fit_json(self):
        result = self.build()
        self.assertFalse(engine.validate(self.draft(result, current=False), self.catalogs)["valid"])
        read = legacy.read_json
        def guarded(path):
            self.assertNotIn("rules_fit", str(path))
            self.assertNotIn("voucher_samples", str(path))
            return read(path)
        with patch.object(legacy, "read_json", side_effect=guarded):
            rows, issues = svc.build_voucher_rows([self.record], self.masters, self.catalogs, "内销")
            self.assertEqual(rows, result["rows"])
            self.assertEqual(issues, [])


    def test_demo_never_queries_erp(self):
        with patch.object(svc, "OFFLINE", True), patch.object(svc.subprocess, "run") as run:
            svc.fetch_missing_domestic_buyers({"id": "demo"}, [{**self.record, "客户名称_脚本用": ""}])
            run.assert_not_called()

    def order_cache(self, records, fetched_at=None):
        value = {"source": orders.SOURCE, "complete": True, "records": records,
                 "modules": {module: {"complete": True} for module in ("domestic", "export")},
                 "fetched_at": fetched_at or dt.datetime.now(dt.timezone.utc).isoformat()}
        orders.save_cache(value, self.mapping)
        return value

    def order(self, code="26MT-TEMPLATE", customer="上海辉懿机械有限公司", order_id="order-1"):
        return {"order_id": order_id, "order_code": code, "customer_name": customer,
                "customer_id": "client-1", "company_name": "嘉兴迈拓不锈钢有限公司"}

    def test_order_customer_overrides_source_name_but_preserves_raw_source_and_manual_override(self):
        record = {**self.record, "客户名称_脚本用": "上海宝冶集团有限公司"}
        self.order_cache([self.order()])
        result = self.build([record])
        self.assertEqual(result["rows"][0]["客户编码"], "010243")
        self.assertEqual(record["客户名称_脚本用"], "上海宝冶集团有限公司")
        self.assertEqual(result["effective_source"][0]["客户名称_来源"], "睿贝ERP内销订单")
        manual = {**record, "_manual_customer_name": True}
        self.assertEqual(self.build([manual])["rows"][0]["客户编码"], "010251")

    def test_joint_add_orders_and_explicit_conflicts(self):
        cache = self.order_cache([self.order("PI-26MT-DP014-ADD2&ADD3")])
        for contract in ("26MT-DP014ADD2", "26MT-DP014 ADD3", "PI-26MT-DP014-ADD2"):
            self.assertEqual(orders.resolve(cache, contract)["公司名称"], "上海辉懿机械有限公司")
        self.assertEqual(orders.resolve(cache, "26MT-DP014ADD20")["状态"], "未找到内销订单")
        cache["records"].append(self.order("26MT-DP014ADD2", "上海宝冶集团有限公司", "order-2"))
        self.assertEqual(orders.resolve(cache, "26MT-DP014ADD2")["状态"], "内销订单客户冲突")
        self.order_cache(cache["records"])
        result = self.build([{**self.record, "合同号_1": "26MT-DP014ADD2"}])
        self.assertEqual(result["rows"][0]["客户编码"], "")
        self.assertIn("存在不同客户", str(result["issues"]))

    def test_add_inheritance_is_used_only_without_a_direct_order(self):
        cache = self.order_cache([self.order("26MT-BASE")])
        self.assertEqual(orders.resolve(cache, "26MT-BASE-ADD1")["继承自"], "26MT-BASE")
        cache["records"].append(self.order("26MT-BASE-ADD1", "上海宝冶集团有限公司", "order-2"))
        result = orders.resolve(cache, "26MT-BASE-ADD1")
        self.assertEqual(result["公司名称"], "上海宝冶集团有限公司")
        self.assertEqual(result["继承自"], "")

    def test_old_approval_cache_and_incomplete_snapshot_are_not_reused(self):
        self.mapping.write_text(json.dumps({"results": [{"合同号_1": "26MT-TEMPLATE",
            "状态": "已获取", "公司名称": "上海宝冶集团有限公司"}]}), encoding="utf-8")
        self.assertEqual(orders.load_cache(self.mapping), {})
        self.assertEqual(self.build()["rows"][0]["客户编码"], "010243")
        orders.save_cache({"source": orders.SOURCE, "complete": False, "records": [self.order()]}, self.mapping)
        self.assertEqual(orders.load_cache(self.mapping), {})

    def test_parse_orders_uses_field_names_instead_of_column_positions(self):
        columns = [
            {"beanName": "companyName", "columnValues": ["另一个公司主体"]},
            {"beanName": "customerName,comId", "columnValues": ["实际客户", "C01"]},
            {"beanName": "orderCode,orderId,tickId", "columnValues": ["PI-26MT-X", "O01", ""]},
        ]
        rows, total = orders.parse_rows({"total": 1, "root": [list(reversed(columns))]})
        self.assertEqual((rows[0]["customer_name"], rows[0]["company_name"], total), ("实际客户", "另一个公司主体", 1))
        with self.assertRaises(ValueError):
            orders.parse_rows({"total": 1, "root": [[columns[0]]]})

    def test_export_orders_use_com_name_and_do_not_change_accounting_type(self):
        columns = [{"beanName": "orderId,orderCode", "columnValues": ["O01", "PI-26MT-03X499"]},
                   {"beanName": "comName,comId", "columnValues": ["上海辉懿机械有限公司", "C01"]},
                   {"beanName": "companyName", "columnValues": ["公司主体不是客户"]}]
        rows, _ = orders.parse_rows({"total": 1, "root": [columns]}, "export")
        summary = [{"beanName": "orderCode,orderId", "columnValues": ["合计", ""]}]
        self.assertEqual(len(orders.parse_rows({"total": 1, "root": [columns, summary]}, "export")[0]), 1)
        with self.assertRaises(ValueError):
            orders.parse_rows({"total": 1, "root": [[{"beanName": "orderCode", "columnValues": ["26MT-03X499"]}]]}, "export")
        self.assertEqual(rows[0]["customer_name"], "上海辉懿机械有限公司")
        cache = self.order_cache(rows)
        self.assertEqual(orders.resolve(cache, "26MT-03X499")["来源类型"], "外销订单客户名称")
        result = self.build([{**self.record, "合同号_1": "26MT-03X499", "客户名称_脚本用": ""}])
        self.assertEqual(result["rows"][0]["客户编码"], "010243")
        self.assertEqual(len(result["rows"]), 8)
        self.assertEqual(result["effective_source"][0]["销售方式"], "内销")
        self.assertEqual(result["effective_source"][0]["客户名称_来源"], "睿贝ERP外销订单")

    def test_numbered_domestic_orders_use_export_customer_and_english_label(self):
        erp_order = {**self.order("26MT-03X499", "KOCH Engineered Solution"), "module": "export"}
        self.order_cache([erp_order])
        record = {**self.record, "合同号_1": "26MT-03X499", "客户名称_脚本用": ""}
        result = self.build([record])
        self.assertEqual(result["rows"][0]["客户编码"], "010249")
        self.assertEqual(len(result["rows"]), 8)
        labels = svc.customer_labels(result["effective_source"], self.masters)
        self.assertEqual(labels, {"010249": "KOCH Engineered Solution"})
        for month in range(1, 13):
            numbered = {**record, "合同号_1": f"26MT-{month:02}X499", "客户名称_脚本用": "KOCH Engineered Solution"}
            self.assertEqual(self.masters.customer_for_record(numbered), "010249")
        self.assertIsNone(self.masters.customer_for_record({**record, "合同号_1": "26MT-DP014", "客户名称_脚本用": "KOCH Engineered Solution"}))
        manual = {**result["effective_source"][0], "_manual_customer_name": True}
        self.assertEqual(svc.customer_labels([manual], self.masters), {})

    def test_cross_module_ids_do_not_collide_and_customer_conflicts_block(self):
        domestic_order = {**self.order("26MT-03X499"), "module": "domestic"}
        export_order = {**self.order("26MT-03X499", "上海宝冶集团有限公司"), "module": "export"}
        cache = self.order_cache([domestic_order, export_order])
        self.assertEqual(len(orders.load_cache(self.mapping)["records"]), 2)
        self.assertEqual(orders.resolve(cache, "26MT-03X499")["状态"], "内销订单客户冲突")
        self.assertEqual(orders.resolve(cache, "26MT-03X499ADD1")["公司名称"], "")
        cache["records"][1]["order_code"] = "26MT-03X499ADD1"
        self.assertEqual(orders.resolve(cache, "26MT-03X499ADD1")["公司名称"], "上海宝冶集团有限公司")

    def test_worker_upgrades_old_domestic_only_cache(self):
        cache = self.order_cache([self.order()])
        cache.pop("modules")
        orders.save_cache(cache, self.mapping)
        with patch.object(svc, "ROOT", Path(self.temp.name)), patch.object(svc, "OFFLINE", False), \
                patch.object(svc, "checkpoint"), patch.object(svc.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            svc.fetch_missing_domestic_buyers({"id": "old-cache"}, [self.record])
        self.assertIn("--full", run.call_args.args[0])

    def test_order_download_resumes_pages_and_rejects_duplicate_ids(self):
        from scripts import fetch_ruibei_buyers as erp
        checkpoint = Path(self.temp.name) / "orders.checkpoint.json"
        first, second = self.order(), self.order(order_id="order-2")
        orders.save_cache({"fingerprint": "account", "records": [first], "total": 2,
                           "next_page": 2, "complete": False}, checkpoint)
        with patch.object(erp, "fetch_page", return_value=([second], 2)) as fetch:
            rows, total = erp.download(None, "endpoint", {}, checkpoint, "account")
        self.assertEqual((len(rows), total), (2, 2))
        self.assertEqual(fetch.call_args.args[3], 2)
        with patch.object(erp, "fetch_page", side_effect=[([first], 2), ([first], 2)]):
            with self.assertRaisesRegex(RuntimeError, "重复ID"):
                erp.download(None, "endpoint", {})

    def test_worker_reuses_full_cache_then_queries_only_a_stale_miss(self):
        cache = self.order_cache([self.order()])
        task = {"id": "order-request"}
        with patch.object(svc, "OFFLINE", False), patch.object(svc.subprocess, "run") as run:
            svc.fetch_missing_domestic_buyers(task, [self.record])
            run.assert_not_called()
        cache["fetched_at"] = "2000-01-01T00:00:00+00:00"
        orders.save_cache(cache, self.mapping)
        unknown = {**self.record, "合同号_1": "26MT-NEW"}
        with patch.object(svc, "ROOT", Path(self.temp.name)), patch.object(svc, "OFFLINE", False), \
                patch.object(svc, "checkpoint"), patch.object(svc.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            svc.fetch_missing_domestic_buyers(task, [self.record, unknown])
        command = run.call_args.args[0]
        self.assertNotIn("--full", command)
        self.assertTrue(str(Path(self.temp.name) / ".chche") in command[command.index("--checkpoint") + 1])
        self.assertFalse(list((Path(self.temp.name) / ".chche").glob("order-request-*.json")))

    def test_worker_creates_full_cache_when_no_snapshot_exists(self):
        with patch.object(svc, "ROOT", Path(self.temp.name)), patch.object(svc, "OFFLINE", False), \
                patch.object(svc, "checkpoint"), patch.object(svc.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            svc.fetch_missing_domestic_buyers({"id": "cold-start"}, [self.record])
        self.assertIn("--full", run.call_args.args[0])

    def test_customer_issue_identifies_blank_order_name_and_missing_archive(self):
        for customer, phrase in [("", "客户名称字段为空"), ("Unregistered company", "未匹配用友客户档案")]:
            self.order_cache([self.order(customer=customer)])
            result = self.build([{**self.record, "客户名称_脚本用": ""}])
            self.assertIn(phrase, str(result["issues"]))





    def test_erp_failures_distinguish_dependency_config_and_network_without_leaking_output(self):
        missing = {**self.record, "客户名称_脚本用": ""}
        for detail, expected in [("ModuleNotFoundError: playwright", "Python依赖缺失"),
                                 ("KeyError: RUI_BEI_ERP_NAME", "ERP配置项缺失"),
                                 ("net::ERR_CONNECTION_REFUSED", "ERP网络访问失败")]:
            with self.subTest(expected=expected), patch.object(svc, "ROOT", Path(self.temp.name)), \
                    patch.object(svc, "OFFLINE", False), patch.object(svc, "checkpoint"), \
                    patch.object(svc.subprocess, "run", return_value=SimpleNamespace(
                        returncode=1, stderr=(detail + " private-value-never-echo").encode())), \
                    self.assertLogs(level="ERROR") as logs:
                with self.assertRaisesRegex(ValueError, expected) as error:
                    svc.fetch_missing_domestic_buyers({"id": "failure"}, [missing])
                self.assertNotIn("private-value-never-echo", str(error.exception))
                self.assertNotIn("private-value-never-echo", str(logs.output))

    def test_cli_writer_excludes_invalid_and_numeric_excel_roundtrip(self):
        good = self.build()
        bad = self.build([{**self.record, "采购金额": ""}])
        target = Path(self.temp.name) / "template.xls"
        with patch.dict(os.environ, {"VOUCHER_MAKER": "内销制单人"}):
            domestic.write_excel({"generated": [good, bad]}, target, dt.date(2026, 10, 26))
        sheet = xlrd.open_workbook(str(target)).sheet_by_index(0)
        header = [str(v).lstrip("* ") for v in sheet.row_values(0)]
        self.assertEqual(sheet.nrows, 9)
        self.assertEqual(sheet.ncols, 53)
        self.assertEqual(sheet.cell_value(1, header.index("制单人")), "内销制单人")
        self.assertEqual(sheet.cell_value(1, header.index("借方金额")), 1130)
        self.assertEqual(sheet.cell_value(4, header.index("借方数量")), 10)

    def test_historical_comparison_never_changes_generated_draft(self):
        generated = domestic.generate_all({"records": [self.record]}, self.masters)
        before = copy.deepcopy(generated)
        comparison.evaluate(generated, {"samples": []})
        comparison.evaluate_full_lines(generated, {"samples": []}, [])
        self.assertEqual(generated, before)


if __name__ == "__main__":
    unittest.main()
