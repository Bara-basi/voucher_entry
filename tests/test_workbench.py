"""Run with: D:\\python\\python.exe -m unittest discover -s tests -v."""
import copy
import datetime as dt
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import xlrd
from fastapi import HTTPException

from app import settings
from app.services import draft_service as engine
from app.services import domestic_voucher_service as domestic
from app.services import feishu_service as feishu
from app.services import voucher_service as legacy
from app.services import workbench_service as svc
from app.services import workbench_store as store
from app import main as legacy_api


class WorkbenchTests(unittest.TestCase):
    def test_voucher_maker_uses_environment_and_reaches_template(self):
        with patch.dict(os.environ, {"VOUCHER_MAKER": ""}):
            self.assertEqual(settings.voucher_maker(), "马云凤")
        with patch.dict(os.environ, {"VOUCHER_MAKER": "环境制单人"}):
            self.assertEqual(settings.voucher_maker(), "环境制单人")
            path = self.path / "environment-maker.xls"
            legacy.write_template_xls(self.draft["rows"], path, dt.date(2026, 10, 28))
            sheet = xlrd.open_workbook(str(path)).sheet_by_index(0)
            header = [str(value).lstrip("* ") for value in sheet.row_values(0)]
            self.assertEqual(sheet.cell_value(1, header.index("制单人")), "环境制单人")

    def test_koch_auto_mapping_cannot_override_verified_customer(self):
        alias = {"id": "old-auto", "alias": "KOCH Engineered Solution",
                 "platform_code": "000062", "enabled": True,
                 "basis": legacy.AUTO_CUSTOMER_BASIS}
        with store.connect(True) as db:
            store.put(db, "settings", "mappings", {
                "version": 1, "items": {"customers": [alias]}})
            state = svc.persist_mapping_migrations(db)
        self.assertEqual(state["version"], 2)
        corrected = next(r for r in state["items"]["customers"] if r["alias"] == alias["alias"])
        self.assertEqual(corrected["platform_code"], "010249")
        with store.connect() as db:
            masters, _ = svc.masters_for(db)
        self.assertEqual(masters.customer_code([alias["alias"]]), "010249")
        records = [r for r in self.source if legacy.to_text(r.get("合同号_1")) == "26MT-03P256"]
        self.assertEqual(len(records), 5)
        rows, _ = engine.build(records, masters, self.catalogs)
        self.assertEqual(rows[0]["客户编码"], "010249")
        valid_records = copy.deepcopy(self.sample["source"])
        for record in valid_records:
            record["买方英文名"] = records[0]["买方英文名"]
            record["客户名称_脚本用"] = records[0]["客户名称_脚本用"]
        self.assertEqual(legacy.build_voucher(valid_records, masters)[0]["客户编码"], "010249")
        export_rows, _ = engine.build(valid_records, masters, self.catalogs)
        path = self.path / "koch.xls"
        svc.write_excel([{**self.sample, "rows": export_rows}], path)
        sheet = xlrd.open_workbook(str(path)).sheet_by_index(0)
        self.assertEqual(sheet.cell_value(1, sheet.row_values(0).index("客户编码")), "010249")

        self.assertEqual(domestic.customer_for_records(records, masters), "010249")
        # Explicit finance edits must survive future automatic migrations.
        svc.save_mapping("customers", {"version": 2, "item": {
            **corrected, "platform_code": "000062"}}, "finance")
        with store.connect(True) as db:
            manual = svc.persist_mapping_migrations(db)
        edited = next(r for r in manual["items"]["customers"] if r["alias"] == alias["alias"])
        self.assertEqual(edited["platform_code"], "000062")
        self.assertEqual(edited["basis"], "财务人工维护")

    def test_customer_primary_source_and_group_consistency(self):
        good = {"record_id": "buyer-1", "销售方式": "出口",
                "买方英文名": [{"text": "KOCH Engineered Solution"}],
                "客户名称_脚本用": {"value": [{"text": "上海辉懿机械有限公司"}]}}
        masters = copy.deepcopy(self.masters)
        self.assertEqual(masters.customer_for_record(good), "010249")
        unknown = {**good, "买方英文名": "Unknown buyer never confirmed"}
        self.assertIsNone(masters.customer_for_record(unknown))
        fitted = {"mappings": {"customers": {
            legacy.normalize_name(unknown["买方英文名"]): "000062"}}}
        self.assertEqual(domestic.customer_for_records([{
            **unknown, "客户名称_脚本用": unknown["买方英文名"]}], masters), "")
        self.assertEqual(masters.customer_for_record({**good, "买方英文名": ""}), "010243")
        conflict = {**good, "record_id": "buyer-2", "买方英文名": "上海辉懿机械有限公司"}
        for kind in ("外销", "内销"):
            records = [copy.deepcopy(self.source[0]) for _ in range(2)]
            for row, buyer in zip(records, (good, conflict)):
                row.update(buyer, 销售方式="内销" if kind == "内销" else "出口")
                row["客户名称_脚本用"] = row["买方英文名"]
            with patch.object(domestic, "RUI_BEI_BUYER_MAPPING", self.path / "no-order-cache.json"):
                rows, issues = svc.build_voucher_rows(records, masters, self.catalogs, kind)
            self.assertEqual(rows[0]["客户编码"], "")
            self.assertTrue(any(i.get("field") == "客户编码" for i in issues))

    def test_customer_ambiguity_does_not_pick_numeric_code(self):
        masters = copy.deepcopy(self.masters)
        masters.customer_by_name = {"ambiguousbuyer": {"123", "A-1"}}
        masters.customer_shortname = {"123": "First", "A-1": "Second"}
        masters.customer_aliases = {}
        masters.customer_history = {}
        self.assertIsNone(masters.customer_code(["Ambiguous Buyer"]))
        masters.customer_by_name = {"exactbuyer": {"123"}}
        masters.customer_history = {"exactbuyer": "A-1"}
        self.assertEqual(masters.customer_code(["Exact Buyer"]), "123")

    def test_add_order_does_not_inherit_first_of_conflicting_buyers(self):
        records = [
            {"合同号_1": "26MT-TEST", "买方英文名": "Buyer A"},
            {"合同号_1": "26MT-TEST", "买方英文名": "Buyer B"},
            {"合同号_1": "26MT-TEST-ADD", "买方英文名": ""},
        ]
        result, _ = legacy.apply_add_inheritance(records)
        self.assertEqual(result[2]["买方英文名"], "")

    def test_old_automatic_customer_is_rechecked_at_both_export_boundaries(self):
        old = copy.deepcopy(self.draft)
        old["id"] = "old-customer"
        old["business_type"] = "外销"
        for record in old["source"]:
            record.update(买方英文名="KOCH Engineered Solution", 客户名称_脚本用="KOCH Engineered Solution")
        old["effective_source"] = copy.deepcopy(old["source"])
        old["rows"][0]["客户编码"] = "000062"
        old["manual_fields"] = []
        self.assertTrue(engine.validate(old, self.catalogs)["valid"])
        with store.connect(True) as db:
            store.put(db, "draft", old["id"], old)
        plan = svc.export_plan([old["id"]])
        self.assertEqual(plan["valid"], [])
        self.assertTrue(any(i["id"].startswith("customer-current:") for i in plan["excluded"][0]["issues"]))
        task = self.task("export", {"drafts": [{"id": old["id"], "version": old["version"]}]})
        with self.assertRaises(HTTPException) as error:
            svc.export_job(task)
        self.assertEqual(error.exception.status_code, 422)
        self.assertFalse(svc.OUTPUT.exists())
        with store.connect(True) as db:
            store.put(db, "export", "old-export", {"id": "old-export", "drafts": [old],
                "record_ids": [r["record_id"] for r in old["source"]],
                "writeback": "pending", "offline": False, "written": 0, "updates": []})
        with patch.object(svc.feishu, "batch_update") as update:
            svc.writeback("old-export")
            update.assert_not_called()
        with store.connect() as db:
            self.assertEqual(store.get(db, "export", "old-export")["writeback"], "failed")
        # A finance user's explicit customer choice remains reviewable and authoritative.
        old["manual_fields"] = [old["rows"][0]["id"] + "/客户编码"]
        with store.connect(True) as db:
            store.put(db, "draft", old["id"], old)
        self.assertEqual(len(svc.export_plan([old["id"]])["valid"]), 1)

    def test_script_customer_alias_does_not_promote_auto_or_disabled_rows(self):
        from scripts import customer_matching
        row = {"alias": "KOCH Engineered Solution", "platform_code": "000062",
               "basis": legacy.AUTO_CUSTOMER_BASIS}
        for mapping in (row, {**row, "basis": "财务人工维护", "enabled": False}):
            with patch.object(customer_matching, "load_mapping", return_value={"aliases": [mapping]}):
                self.assertIsNone(customer_matching.confirmed_alias(row["alias"], self.masters.customers))
        with patch.object(customer_matching, "load_mapping", return_value={"aliases": [
                {**row, "basis": "财务人工维护", "platform_code": "010249"}]}):
            self.assertEqual(customer_matching.confirmed_alias(row["alias"], self.masters.customers)["code"], "010249")

    def test_unchanged_catalog_and_mapping_saves_are_noops(self):
        for kind in ("inventory", "suppliers", "customers"):
            with store.connect() as db:
                state = svc.mapping_state(db)
                row = copy.deepcopy(state["items"][kind][0])
            result = svc.save_mapping(kind, {"version": state["version"], "item": row}, "test")
            self.assertEqual(result["version"], state["version"])
        for kind in ("subjects", "aux", "rates"):
            row = copy.deepcopy(svc.catalog()[kind][0])
            if kind == "rates":
                row["fields"]["币种"] = legacy.to_text(row["fields"]["币种"])
                row["fields"]["汇率"] = str(row["fields"]["汇率"]) + "0" if "." in str(row["fields"]["汇率"]) else str(row["fields"]["汇率"]) + ".0"
            result = svc.save_catalog(kind, {"version": 1, "key": svc.catalog_key(kind, row), "item": row}, "test")
            self.assertFalse(result["changed"], kind)
        with store.connect() as db:
            self.assertEqual(svc.mapping_state(db)["version"], 1)
            self.assertEqual(store.all_items(db, "audit"), [])

    def test_mapping_recalculation_preserves_manual_fields_and_deleted_rows(self):
        draft = copy.deepcopy(self.draft)
        draft["rows"][0]["摘要"] = "财务核对过的摘要"
        draft = svc.save_draft(draft["id"], draft, "test")
        with store.connect(True) as db:
            state = svc.mapping_state(db); state["version"] += 1
            store.put(db, "settings", "mappings", state)
        result = svc.recalculate_mappings({**svc.mapping_recalculation_plan(), "preserve_manual": True}, "test")[0]
        self.assertEqual(result["rows"][0]["摘要"], "财务核对过的摘要")
        self.assertTrue(result["manual_fields"])
        result["rows"].pop()
        result = svc.save_draft(result["id"], result, "test")
        rebuilt = svc.recalculate(result["id"], {"version": result["version"], "preserve_manual": True}, "test")
        self.assertEqual(rebuilt["rows"], result["rows"])

    def test_explicit_status_conflict_selection_can_build(self):
        records = copy.deepcopy(self.draft["source"])
        records[0]["是否开凭证"] = "待开"
        records.append({**records[0], "record_id": "already-open", "是否开凭证": "已开"})
        result = svc.query_job(self.task("query", {"mode": "pending"}, {"records": records, "fetched": True}))
        with store.connect() as db:
            snap = store.get(db, "snapshot", result["snapshot_id"])
        group = snap["groups"][0]
        self.assertTrue(group["blocked"])
        self.assertEqual(group["hard_blocked"], [])
        payload = {"snapshot_id": snap["id"], "keys": [group["key"]], "posting_date": "2026-10-22"}
        with self.assertRaises(HTTPException):
            svc.build_job(self.task("build", payload))
        payload["acknowledged_groups"] = [group["key"]]
        built = svc.build_job(self.task("build", payload))
        with store.connect() as db:
            batch = store.get(db, "batch", built["batch_id"])
            draft = store.get(db, "draft", batch["draft_ids"][0])
        self.assertEqual(draft["status_acknowledgement"]["session"], "test-browser")
        # Explicitly reopening an exported draft must remain visible in the workbench.
        with store.connect(True) as db:
            draft["exports"] = ["prior-export"]
            store.put(db, "draft", draft["id"], draft)
        reopen = self.task("build", payload)
        reopen["id"] = "build-reopen"
        svc.build_job(reopen)
        with store.connect() as db:
            self.assertTrue(any(draft["id"] in b["draft_ids"] for b in svc.workbench_batches(db)))
        group["hard_blocked"] = ["缺少报关单号，请在飞书补充后重新检索"]
        with store.connect(True) as db:
            store.put(db, "snapshot", snap["id"], snap)
        with self.assertRaises(HTTPException):
            svc.build_job(self.task("build", payload))

    def test_manual_exception_blocks_export_and_persists(self):
        payload = copy.deepcopy(self.draft)
        payload["manual_exception"] = True
        saved = svc.save_draft(payload["id"], payload, "finance")
        self.assertFalse(saved["valid"])
        self.assertEqual(len(svc.export_plan([saved["id"]])["excluded"]), 1)
        rebuilt = svc.recalculate(saved["id"], {"version": saved["version"]}, "finance")
        self.assertTrue(rebuilt["manual_exception"])
        rebuilt["manual_exception"] = False
        fixed = svc.save_draft(rebuilt["id"], rebuilt, "finance")
        self.assertTrue(fixed["valid"], fixed["issues"])

    def test_deleted_row_clears_only_its_build_issues(self):
        draft = copy.deepcopy(self.draft)
        row = {**draft["rows"][0], "id": "zero-row", "借方金额": "0", "贷方金额": "0",
               "原币借方": "0", "原币贷方": "0"}
        draft["rows"].append(row)
        draft["build_issues"].append({"id": "zero", "row_id": row["id"], "field": "借方金额",
                                      "severity": "error", "message": "无效行"})
        with store.connect(True) as db:
            store.put(db, "draft", draft["id"], draft)
        self.assertFalse(engine.validate(draft, self.catalogs)["valid"])
        draft["rows"].pop()
        saved = svc.save_draft(draft["id"], draft, "finance")
        self.assertTrue(saved["valid"], saved["issues"])
        saved["rows"].pop(0)
        self.assertFalse(svc.save_draft(saved["id"], saved, "finance")["valid"])
        saved["blocking_issues"] = [{"id": "global", "severity": "error", "message": "来源冲突"}]
        self.assertTrue(any(i["id"] == "global" for i in engine.validate(saved, self.catalogs)["issues"]))

    def test_mapping_bulk_atomic_manual_protection(self):
        with store.connect(True) as db:
            state = svc.mapping_state(db)
            state["version"] += 1
            store.put(db, "settings", "mappings", state)
            other = {**copy.deepcopy(self.draft), "id": "other", "manual_fields": ["__rows__/structure"]}
            store.put(db, "draft", "other", other)
            store.put(db, "draft", "exported", {**copy.deepcopy(self.draft), "id": "exported", "exports": ["file"]})
        plan = svc.mapping_recalculation_plan()
        self.assertEqual(len(plan["drafts"]), 2)
        # Place the manual draft second to prove that a prior recalculation rolls back.
        plan["drafts"].sort(key=lambda d: d["manual"])
        with self.assertRaises(HTTPException):
            svc.recalculate_mappings(plan, "finance")
        with store.connect() as db:
            self.assertEqual(store.get(db, "draft", self.draft["id"])["version"], 1)
        stale = copy.deepcopy(plan)
        stale["drafts"][0]["version"] = 999
        with self.assertRaises(HTTPException):
            svc.recalculate_mappings({**stale, "replace_manual": True}, "finance")
        results = svc.recalculate_mappings({**plan, "replace_manual": True}, "finance")
        self.assertTrue(all(not d["mapping_stale"] and d["mapping_version"] == 2 for d in results))
        self.assertEqual(svc.mapping_recalculation_plan()["drafts"], [])
        with store.connect() as db:
            self.assertEqual(store.get(db, "draft", "exported")["version"], 1)

    def test_filtered_pending_group_resume_and_parent_dependencies(self):
        pending = {**self.draft["source"][0], "record_id": "pending", "合同号_1": "26MT-01T001Y-ADD",
                   "报关单号": "group", "是否开凭证": "待开"}
        opened = {**pending, "record_id": "opened", "是否开凭证": "已开"}
        parent = {**pending, "record_id": "parent", "合同号_1": "26MT-01T001", "报关单号": "parent-group"}
        task = self.task("query", {"mode": "pending"})
        calls = []
        def response(records, more=False):
            return {"code": 0, "data": {"items": [{"record_id": r["record_id"], "fields": r} for r in records],
                                        "has_more": more, "page_token": "next" if more else None}}
        def request(url, **kwargs):
            calls.append((url, kwargs))
            self.assertEqual(kwargs["method"], "POST")
            condition = kwargs["data"]["filter"]["conditions"][0]
            if condition["field_name"] == "是否开凭证":
                self.assertEqual(condition["value"], ["待开"])
                return response([pending])
            if condition["field_name"] == "报关单号":
                if "page_token=next" not in url:
                    return response([pending], True)
                return response([opened])
            return response([parent])
        with patch.object(svc.feishu, "_request", side_effect=request):
            records = svc.filtered_pending_records(task, "mock/records", "mock")
            count = len(calls)
            self.assertEqual(svc.filtered_pending_records(task, "mock/records", "mock"), records)
            self.assertEqual(len(calls), count)
        self.assertEqual({r["record_id"] for r in records}, {"pending", "opened"})
        cp = {**task["checkpoint"], "records": records, "fetched": True}
        result = svc.query_job(self.task("query", {"mode": "pending"}, cp))
        with store.connect() as db:
            snapshot = store.get(db, "snapshot", result["snapshot_id"])
        self.assertEqual(len(snapshot["groups"]), 1)
        self.assertTrue(snapshot["groups"][0]["blocked"])
        self.assertEqual({r["record_id"] for r in snapshot["all_records"]}, {"pending", "opened", "parent"})

    def test_local_group_selection_and_reset(self):
        code = svc.catalog()["suppliers"][0]["code"]
        with store.connect() as db:
            version = svc.mapping_state(db)["version"]
        state = svc.save_mapping("suppliers", {"version": version, "item": {
            "alias": "测试分组供应商", "platform_code": code, "enabled": True,
            "group_override": "  新建分组  "}}, "test")
        row = next(r for r in state["items"]["suppliers"] if r["alias"] == "测试分组供应商")
        self.assertEqual(row["group_override"], "新建分组")
        state = svc.save_mapping("suppliers", {"version": state["version"], "item": {
            **row, "group_override": ""}}, "test")
        self.assertEqual(next(r for r in state["items"]["suppliers"] if r["id"] == row["id"])["group_override"], "")
        subject = svc.catalog()["subjects_by_code"]["112202"]
        svc.save_catalog("subjects", {"version": 1, "key": "112202", "item": {
            **subject, "group_override": "收款核对"}}, "test")
        self.assertEqual(svc.catalog()["subjects_by_code"]["112202"]["group_name"], "收款核对")
        svc.save_catalog("subjects", {"version": 2, "key": "112202", "item": {
            **subject, "group_override": None}}, "test")
        self.assertEqual(svc.catalog()["subjects_by_code"]["112202"].get("group_name"), subject.get("group_name"))
        with self.assertRaises(HTTPException):
            svc.save_catalog("subjects", {"version": 3, "key": "112202", "item": {
                **subject, "group_override": "a" * 81}}, "test")

    def test_auxiliary_accounts_use_root_categories_and_exclude_parent_rows(self):
        catalogs = svc.catalog()
        aux = catalogs["aux"]
        codes = {str(row["account_code"]) for row in aux}
        self.assertEqual(len(aux), 47)
        self.assertTrue(codes.isdisjoint({"2001", "200101", "1408", "1221", "1122", "2202"}))
        self.assertEqual(
            {row["group_name"] for row in aux if row["account_code"].startswith("2001")},
            {"2001 · 短期借款"},
        )
        self.assertEqual(
            {row["group_name"] for row in aux if row["account_code"].startswith("1408")},
            {"1408 · 委托加工物资"},
        )
        self.assertNotIn("2001", catalogs["aux_by_code"])
        self.assertIn("20010101", catalogs["aux_by_code"])
        subjects = catalogs["subjects"]
        subject_codes = {str(row["code"]) for row in subjects}
        self.assertEqual(len(subjects), 351)
        self.assertEqual(len({row["group_name"] for row in subjects}), 66)
        self.assertTrue(all(row.get("leaf") for row in subjects))
        self.assertTrue(subject_codes.isdisjoint({"2001", "200101", "1408", "1001", "5602"}))
        self.assertEqual(
            {row["group_name"] for row in subjects if row["code"].startswith("2001")},
            {"2001 · 短期借款"},
        )
        self.assertEqual(
            {row["group_name"] for row in subjects if row["code"].startswith("1408")},
            {"1408 · 委托加工物资"},
        )

    def test_workbench_recent_unexported_batches(self):
        now = dt.datetime(2026, 10, 20, 12, tzinfo=store.TZ)
        with store.connect(True) as db:
            for i in range(25):
                stamp = (now - dt.timedelta(days=i)).isoformat()
                store.put(db, "draft", str(i), {"id": str(i), "created_at": stamp, "exports": []})
                store.put(db, "batch", str(i), {"id": str(i), "created_at": stamp, "draft_ids": [str(i)]})
            store.put(db, "draft", "exported", {"id": "exported", "created_at": now.isoformat(), "exports": ["file"]})
            store.put(db, "batch", "mixed", {"id": "mixed", "created_at": now.isoformat(), "draft_ids": ["0", "exported"]})
            store.put(db, "batch", "done", {"id": "done", "created_at": now.isoformat(), "draft_ids": ["exported"]})
            old = (now - dt.timedelta(days=31)).isoformat()
            store.put(db, "draft", "old", {"id": "old", "created_at": old, "exports": []})
            store.put(db, "batch", "old", {"id": "old", "created_at": old, "draft_ids": ["old"]})
        with store.connect() as db:
            rows = svc.workbench_batches(db, now)
        self.assertEqual(len(rows), 20)
        self.assertFalse(any(b["id"] in ("old", "done") for b in rows))
        self.assertEqual(next(b for b in rows if b["id"] == "mixed")["draft_ids"], ["0"])
        self.assertTrue(all("exported" not in b["draft_ids"] for b in rows))

    def test_local_catalog_edits_and_conflicts(self):
        before = svc.catalog()
        subject = next(r for r in before["subjects"] if r["code"] == "112202")
        svc.save_catalog("subjects", {"version": 1, "key": "112202",
                         "item": {"name": "美元本地名称", "direction": "Debit"}}, "test")
        self.assertEqual(svc.catalog()["subjects_by_code"]["112202"]["name"], "美元本地名称")
        self.assertNotEqual(subject["name"], "美元本地名称")
        with self.assertRaises(HTTPException) as conflict:
            svc.save_catalog("subjects", {"version": 1, "key": "112202",
                             "item": {"name": "冲突", "direction": "Debit"}}, "other")
        self.assertEqual(conflict.exception.status_code, 409)
        original = before["aux_by_code"]["112202"]
        changed = [{**original[0], "mustinput": False}]
        svc.save_catalog("aux", {"version": 2, "key": "112202",
                         "item": {"auxiliary_fields": changed}}, "test")
        self.assertFalse(svc.catalog()["aux_by_code"]["112202"][0]["mustinput"])
        with store.connect() as db:
            self.assertEqual(svc.mapping_state(db)["version"], 3)
            self.assertEqual(len(store.all_items(db, "audit")), 2)

    def test_rate_override_reaches_rule_engine(self):
        record = next(r for r in svc.catalog()["rates"] if r["fields"]["币种"] == ["USD"])
        fields = {**record["fields"], "币种": "USD", "汇率": "7.123456"}
        key = svc.catalog_key("rates", record)
        svc.save_catalog("rates", {"version": 1, "key": key, "item": {"fields": fields}}, "test")
        with store.connect() as db:
            masters, _ = svc.masters_for(db)
            self.assertEqual(str(masters.rates[(fields["实际年份"], fields["实际月份"], "USD")]), "7.123456")
        self.assertEqual(next(r for r in svc.catalog()["rates"] if svc.catalog_key("rates", r) == key)["fields"]["汇率"], "7.123456")
        with self.assertRaises(HTTPException):
            svc.save_catalog("rates", {"version": 2, "item": {"fields": fields}}, "test")
        for invalid in ("NaN", "-1", "0"):
            with self.assertRaises(HTTPException):
                svc.save_catalog("rates", {"version": 2, "key": key,
                                 "item": {"fields": {**fields, "汇率": invalid}}}, "test")

    @classmethod
    def setUpClass(cls):
        cls.catalogs = svc.catalog()
        cls.masters = legacy.MasterData()
        raw = legacy.read_json(svc.ROOT / "data/ai_customs_source.json")["records"]
        cls.source = [
            dict(r["fields"], record_id=r["record_id"])
            for r in raw
            if svc.business_type(r["fields"]) == "外销"
        ]
        domestic_raw = legacy.read_json(
            svc.ROOT / "data/domestic_source_current.json"
        )["records"]
        cls.domestic_source = [
            dict(r.get("fields", r), record_id=r.get("record_id") or r.get("id"))
            for r in domestic_raw
            if svc.business_type(r.get("fields", r)) == "内销"
        ]
        for group in legacy.group_records(cls.source).values():
            if len(group) != 1:
                continue
            rows, issues = engine.build(group, cls.masters, cls.catalogs)
            draft = {"id": "test-draft", "customs_no": group[0]["报关单号"],
                     "contracts": [group[0]["合同号_1"]], "source": group, "version": 1,
                     "header": {"posting_date": "2026-10-20", "maker": "测试制单人", "category": "记"},
                     "rows": rows, "build_issues": issues, "blocking_issues": [], "overrides": {},
                     "manual_fields": [], "exports": [], "mapping_version": 1}
            if engine.validate(draft, cls.catalogs)["valid"]:
                cls.sample = draft
                break
        else:
            raise AssertionError("No valid sample")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        for obj, name, value in ((store, "DB_PATH", self.path / "test.sqlite3"),
                                  (svc, "OUTPUT", self.path / "outputs"), (svc, "OFFLINE", True)):
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        self.draft = copy.deepcopy(self.sample)
        with store.connect(True) as db:
            store.put(db, "draft", self.draft["id"], self.draft)

    def test_generated_source_refresh_is_automatic_and_idempotent(self):
        draft = copy.deepcopy(self.draft)
        draft["incoming_source"] = copy.deepcopy(draft["source"])
        draft["incoming_source"][0]["客户名称来源"] = "更新后的检索"
        draft["incoming_effective_source"] = copy.deepcopy(draft["incoming_source"])
        with store.connect(True) as db:
            store.put(db, "draft", draft["id"], draft)
            updated = svc.refresh_generated_draft(db, draft["id"])
            self.assertNotIn("incoming_source", updated)
            self.assertEqual(updated["source"], draft["incoming_source"])
            self.assertEqual(updated["version"], draft["version"] + 1)
            again = svc.refresh_generated_draft(db, draft["id"])
            self.assertEqual(again["version"], updated["version"])

    def test_automatic_refresh_preserves_manual_and_exported_drafts(self):
        for protected in ({"manual_fields": ["__rows__/structure"]},
                          {"overrides": {"some-record": {"采购金额": 123}}},
                          {"exports": ["saved-export"]}):
            draft = {**copy.deepcopy(self.draft), **protected}
            draft["incoming_source"] = copy.deepcopy(draft["source"])
            with store.connect(True) as db:
                store.put(db, "draft", draft["id"], draft)
                self.assertEqual(svc.refresh_generated_draft(db, draft["id"]), draft)

    def test_template_refresh_preserves_manual_fields_and_header(self):
        draft = copy.deepcopy(self.draft)
        draft.update(business_type="内销", domestic_template_version="old")
        draft["rows"][0]["摘要"] = "人工核对后的摘要"
        draft["manual_fields"] = [draft["rows"][0]["id"] + "/摘要"]
        with store.connect(True) as db, patch.object(svc, "build_voucher_rows", return_value=(copy.deepcopy(self.draft["rows"]), [])):
            store.put(db, "draft", draft["id"], draft)
            updated = svc.refresh_generated_draft(db, draft["id"])
            self.assertEqual(updated["rows"][0]["摘要"], "人工核对后的摘要")
            self.assertEqual(updated["header"], draft["header"])
            self.assertEqual(updated["domestic_template_version"], domestic.TEMPLATE_VERSION)

    def task(self, kind, payload, checkpoint=None):
        task = {"id": kind + "-test", "kind": kind, "payload": payload, "checkpoint": checkpoint or {},
                "session": "test-browser", "created_at": store.now(), "progress": 0, "status": "running"}
        with store.connect(True) as db:
            store.put(db, "task", task["id"], task)
        return task

    def test_manual_edit_autosave_and_conflict(self):
        payload = copy.deepcopy(self.draft)
        payload["rows"][0]["摘要"] = "财务修改摘要"
        saved = svc.save_draft(payload["id"], payload, "browser-a")
        self.assertEqual(saved["version"], 2)
        self.assertTrue(saved["valid"])
        with self.assertRaises(HTTPException) as ctx:
            svc.save_draft(payload["id"], payload, "browser-b")
        self.assertEqual(ctx.exception.status_code, 409)
        with store.connect() as db:
            self.assertEqual(store.get(db, "draft", payload["id"])["rows"][0]["摘要"], "财务修改摘要")
            self.assertEqual(store.all_items(db, "audit")[0]["session"], "browser-a")

    def test_missing_supplier_is_editable_and_becomes_valid(self):
        records = copy.deepcopy(self.draft["source"])
        records[0]["供应商"] = ""; records[0]["供应商简称"] = ""
        rows, issues = engine.build(records, self.masters, self.catalogs)
        draft = copy.deepcopy(self.draft)
        draft.update(rows=rows, build_issues=issues, source=records)
        engine.validate(draft, self.catalogs)
        self.assertFalse(draft["valid"])
        with store.connect(True) as db:
            store.put(db, "draft", draft["id"], draft)
        row = next(r for r in draft["rows"] if r["id"].endswith(":ap"))
        row["供应商编码"] = next(r["供应商编码"] for r in self.sample["rows"] if r["供应商编码"])
        saved = svc.save_draft(draft["id"], draft, "test")
        self.assertTrue(saved["valid"], saved["issues"])

    def test_missing_values_not_silently_zero(self):
        records = copy.deepcopy(self.draft["source"])
        records[0]["不含税成本"] = ""
        rows, issues = engine.build(records, self.masters, self.catalogs)
        self.assertEqual(next(r for r in rows if r["id"].endswith(":stock"))["借方金额"], "")
        records[0]["不含税成本"] = "NaN"
        _, issues = engine.build(records, self.masters, self.catalogs)
        self.assertTrue(any(i["source_field"] == "不含税成本" for i in issues))

    def test_override_requires_recalculation_and_manual_protection(self):
        draft = copy.deepcopy(self.draft)
        draft["rows"][0]["摘要"] = "人工摘要"
        draft["overrides"] = {draft["source"][0]["record_id"]: {"供应商简称": "新简称"}}
        result = svc.save_draft(draft["id"], draft, "test")
        self.assertFalse(result["valid"])
        with self.assertRaises(HTTPException):
            svc.recalculate(draft["id"], {"version": result["version"]}, "test")
        result = svc.recalculate(draft["id"], {"version": result["version"], "replace_manual": True}, "test")
        self.assertEqual(result["manual_fields"], [])
        self.assertEqual(result["calculated_overrides"], result["overrides"])

    def test_decimal_balance_and_invalid_codes(self):
        d = copy.deepcopy(self.draft)
        d["rows"][0]["借方金额"] = str(engine.number(d["rows"][0]["借方金额"]) + engine.Decimal(".01"))
        engine.validate(d, self.catalogs)
        self.assertEqual(d["totals"]["difference"], "0.01")
        d["rows"][0]["客户编码"] = "does-not-exist"
        engine.validate(d, self.catalogs)
        self.assertTrue(any(i["field"] == "客户编码" for i in d["issues"]))

    def test_reordered_rows_are_protected_from_recalculation(self):
        draft = copy.deepcopy(self.draft)
        draft["rows"].reverse()
        saved = svc.save_draft(draft["id"], draft, "test")
        self.assertIn("__rows__/structure", saved["manual_fields"])
        with self.assertRaises(HTTPException):
            svc.recalculate(draft["id"], {"version": saved["version"]}, "test")

    def test_group_completion_conflicts_and_missing_identity(self):
        base = copy.deepcopy(self.draft["source"][0])
        base.update(record_id="a", 报关单号="group", 是否开凭证="待开",
                    出口日期=dt.datetime(2026, 9, 30, 23, 59, 59, tzinfo=store.TZ).timestamp() * 1000)
        second = dict(base, record_id="b", 是否开凭证="", 出口日期=dt.datetime(2026, 10, 1, tzinfo=store.TZ).timestamp() * 1000)
        task = self.task("query", {"mode": "pending"},
                         {"records": [base, second], "fetched": True})
        result = svc.query_job(task)
        with store.connect() as db:
            snap = store.get(db, "snapshot", result["snapshot_id"])
        self.assertEqual(snap["groups"][0]["added"], 1)
        self.assertFalse(snap["groups"][0]["blocked"])
        task["checkpoint"]["records"][1]["是否开凭证"] = "已开"
        svc.query_job(task)
        with store.connect() as db:
            self.assertTrue(store.get(db, "snapshot", task["id"])["groups"][0]["blocked"])
        task["payload"] = {"mode": "pending"}
        task["checkpoint"]["records"] = [dict(base, 报关单号="")]
        svc.query_job(task)
        with store.connect() as db:
            self.assertIn("缺少报关单号", store.get(db, "snapshot", task["id"])["groups"][0]["blocked"][0])

    def test_date_query_is_rejected(self):
        task = self.task("query", {"mode": "date", "start_date": "2026-01-01", "end_date": "2026-12-31"})
        with self.assertRaises(HTTPException) as ctx:
            svc.query_job(task)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("已停用", ctx.exception.detail)

    def test_empty_pending_result_returns_a_completed_empty_snapshot(self):
        opened = dict(self.draft["source"][0], record_id="opened-only", 是否开凭证="已开")
        task = self.task("query", {"mode": "pending"},
                         {"records": [opened], "fetched": True})
        result = svc.query_job(task)
        self.assertEqual(result["groups"], 0)
        self.assertEqual(result["matched"], 0)
        with store.connect() as db:
            snapshot = store.get(db, "snapshot", result["snapshot_id"])
        self.assertEqual(snapshot["groups"], [])
        self.assertEqual(snapshot["excluded"], 1)

    def test_legacy_domestic_and_date_bulk_interfaces_are_explicitly_retired(self):
        for sale_method in ("内销", "视同销售", "预录"):
            with self.assertRaises(HTTPException) as ctx:
                legacy_api.list_groups(sale_method=sale_method)
            self.assertEqual(ctx.exception.status_code, 422)
        with self.assertRaises(HTTPException) as ctx:
            legacy_api.generate_bulk_retired()
        self.assertEqual(ctx.exception.status_code, 410)

    def test_task_boundary_validation_happens_before_queueing(self):
        cases = [
            ("query", {"mode": "date"}),
            ("query", {"mode": "pending", "invalid": float("nan")}),
            ("build", {"snapshot_id": "", "keys": [], "posting_date": "2026-10-23"}),
            ("build", {"snapshot_id": "ok", "keys": ["a", "a"], "posting_date": "2026-10-23"}),
            ("build", {"snapshot_id": "ok", "keys": ["a"], "posting_date": "2026-02-30"}),
            ("export", {"drafts": []}),
            ("export", {"drafts": [{"id": "a", "version": True}]}),
            ("writeback", {}),
        ]
        with store.connect() as db:
            before = {t["id"] for t in store.all_items(db, "task")}
        with patch.object(svc, "submit") as submit:
            for index, (kind, payload) in enumerate(cases):
                with self.assertRaises(HTTPException) as ctx:
                    svc.create_task(kind, payload, "test", "boundary-" + str(index))
                self.assertEqual(ctx.exception.status_code, 422)
            submit.assert_not_called()
        with store.connect() as db:
            self.assertEqual({t["id"] for t in store.all_items(db, "task")}, before)

    def test_non_finite_override_and_oversize_summary_are_blocked(self):
        draft = copy.deepcopy(self.draft)
        draft["overrides"] = {
            draft["source"][0]["record_id"]: {"供应商简称": float("inf")}
        }
        with self.assertRaises(HTTPException) as ctx:
            svc.save_draft(draft["id"], draft, "test")
        self.assertEqual(ctx.exception.status_code, 422)
        draft["overrides"] = {}
        draft["rows"][0]["摘要"] = "x" * 32768
        saved = svc.save_draft(draft["id"], draft, "test")
        self.assertFalse(saved["valid"])
        self.assertTrue(any("32767" in issue["message"] for issue in saved["issues"]))

    def test_legacy_queued_date_task_is_retired_and_audit_history_is_bounded(self):
        old = {"id": "legacy-date", "kind": "query",
               "payload": {"mode": "date", "start_date": "2026-01-01", "end_date": "2026-01-31"},
               "status": "queued", "progress": 0, "total": 0, "created_at": store.now(),
               "updated_at": store.now(), "checkpoint": {}}
        with store.connect(True) as db:
            store.put(db, "task", old["id"], old)
        with patch.object(svc, "submit") as submit:
            svc.resume()
            submit.assert_not_called()
        with store.connect() as db:
            retired = store.get(db, "task", old["id"])
            self.assertEqual(retired["status"], "failed")
            self.assertIn("已停用", retired["message"])
            for index in range(130):
                store.audit(db, "bounded", "test", "edit", None, {"index": index})
        with store.connect() as db:
            entries = store.audit_items(db, "bounded", 100)
        self.assertEqual(len(entries), 100)
        self.assertEqual(entries[0]["after"]["index"], 129)

    def test_contract_search_joint_and_add(self):
        text = engine.contract_index(["PI-26MT-01S180&190&241", "26MT-06H251&ADD1", "26MT-09T039Y-2-鸿迪"])
        for expected in ("26MT-01S190", "26MT-01S241", "26MT-06H251-ADD1", "26MT09T039Y2鸿迪"):
            self.assertIn(expected, text)

    def test_domestic_query_build_and_business_label(self):
        records = [
            {**record, "是否开凭证": "待开"}
            for record in self.domestic_source
            if record.get("合同号_1") == "26MT-DP012"
        ]
        self.assertEqual(len(records), 1)
        records[0]["内陆运费含税"] = "0"  # Fee treatment is a separate pending business rule.
        records[0]["客户名称_脚本用"] = {
            "type": 1,
            "value": [{"text": "上海宝冶集团有限公司", "type": "text"}],
        }
        task = self.task(
            "query",
            {"mode": "pending"},
            {"records": records, "fetched": True},
        )
        result = svc.query_job(task)
        with store.connect() as db:
            snapshot = store.get(db, "snapshot", result["snapshot_id"])
        self.assertEqual(len(snapshot["groups"]), 1)
        group = snapshot["groups"][0]
        self.assertEqual(group["business_type"], "内销")
        self.assertEqual(group["business_no"], "26MT-DP012")
        self.assertEqual(group["customs_no"], "")

        built = svc.build_job(self.task(
            "build",
            {
                "snapshot_id": snapshot["id"],
                "keys": [group["key"]],
                "posting_date": "2026-10-23",
            },
        ))
        with store.connect() as db:
            batch = store.get(db, "batch", built["batch_id"])
            draft = store.get(db, "draft", batch["draft_ids"][0])
        self.assertEqual(draft["business_type"], "内销")
        self.assertEqual(draft["business_no"], "26MT-DP012")
        self.assertTrue(draft["rows"])
        self.assertTrue(draft["valid"], draft["issues"])

    def test_export_saved_revision_and_excel_roundtrip(self):
        task = self.task("export", {"drafts": [{"id": self.draft["id"], "version": 1}]})
        with patch.object(feishu := svc.feishu, "batch_update") as update:
            svc.export_job(task); svc.export_job(task)
            update.assert_not_called()
        path = next(svc.OUTPUT.glob("*.xls"))
        book = xlrd.open_workbook(str(path)); sheet = book.sheet_by_index(0)
        header = [str(sheet.cell_value(0, c)).lstrip("* ") for c in range(sheet.ncols)]
        self.assertEqual(sheet.ncols, 53)
        self.assertEqual(sheet.nrows, len(self.draft["rows"]) + 1)
        for i, row in enumerate(self.draft["rows"], 1):
            for field in ("摘要", "科目编码", "客户编码", "供应商编码", "存货档案", "借方金额", "贷方金额"):
                actual = sheet.cell_value(i, header.index(field))
                expected = float(row[field]) if field in engine.NUMBERS else row[field]
                self.assertEqual(actual, expected)
            self.assertEqual(sheet.cell_value(i, header.index("制单人")), "测试制单人")
        with store.connect() as db:
            exported = store.get(db, "export", task["id"])
        self.assertEqual(exported["writeback"], "offline")
        self.assertEqual(exported["drafts"][0]["version"], 1)
        self.assertEqual(len(exported["record_ids"]), len(self.draft["source"]))

    def test_export_automatically_writes_exact_feishu_status_fields(self):
        task = self.task("export", {
            "drafts": [{"id": self.draft["id"], "version": 1}],
        })
        with patch.object(svc, "OFFLINE", False), \
             patch.object(svc.feishu, "batch_update") as update:
            svc.export_job(task)
        update.assert_called()
        fields = update.call_args.args[0][0]["fields"]
        self.assertEqual(fields, {
            "是否开凭证": "已开",
            "凭证开具日期": self.draft["header"]["posting_date"][:7],
            "凭证开具结果": "保存成功",
        })
        with store.connect() as db:
            exported = store.get(db, "export", task["id"])
        self.assertEqual(exported["writeback"], "done")

    def test_batch_update_rejects_unpersisted_field_values(self):
        response = {
            "code": 0,
            "data": {
                "records": [{
                    "record_id": "record-1",
                    "fields": {
                        "是否开凭证": "待开",
                        "凭证开具日期": "",
                        "凭证开具结果": "",
                    },
                }],
            },
        }
        with patch.object(feishu, "tenant_token", return_value="test-token"), \
             patch.object(feishu, "_request", return_value=response):
            with self.assertRaises(feishu.FeishuError):
                feishu.batch_update([{
                    "record_id": "record-1",
                    "fields": {
                        "是否开凭证": "已开",
                        "凭证开具日期": "2026-10",
                        "凭证开具结果": "保存成功",
                    },
                }])

    def test_writeback_checkpoint_resume(self):
        export = {"id": "resume", "writeback": "pending", "offline": False, "written": 0,
                  "updates": [{"record_id": str(i), "fields": {"是否开凭证": "已开"}} for i in range(201)]}
        with store.connect(True) as db:
            store.put(db, "export", "resume", export)
        with self.assertLogs(level="ERROR"), patch.object(svc.feishu, "batch_update", side_effect=[None, RuntimeError("network")]):
            svc.writeback("resume")
        with store.connect() as db:
            self.assertEqual(store.get(db, "export", "resume")["written"], 100)
        with patch.object(svc.feishu, "batch_update") as update:
            svc.writeback("resume")
            self.assertEqual(len(update.call_args_list), 2)
            self.assertEqual(update.call_args_list[0].args[0][0]["record_id"], "100")
            svc.writeback("resume")
            self.assertEqual(len(update.call_args_list), 2)

    def test_superseded_export_cannot_overwrite_newer_status(self):
        with store.connect(True) as db:
            store.put(db, "export", "old", {"id": "old", "record_ids": ["one"],
                      "writeback": "failed", "written": 0, "offline": False, "updates": []})
            store.put(db, "export", "new", {"id": "new", "record_ids": ["one"]})
        with patch.object(svc.feishu, "batch_update") as update:
            svc.writeback("old")
            update.assert_not_called()
        with store.connect() as db:
            self.assertEqual(store.get(db, "export", "old")["writeback"], "superseded")

    def test_feishu_page_resume_and_source_verification(self):
        (self.path / "data").mkdir()
        records = [dict(self.draft["source"][0], record_id=k, 是否开凭证="待开") for k in ("a", "b")]
        def item(r):
            return {"record_id": r["record_id"], "fields": {k: v for k, v in r.items() if k != "record_id"}}
        def ok(data):
            return {"code": 0, "data": data}
        task = self.task("query", {"mode": "pending"})
        responses = [ok({"app": {"name": "2026年  海关AI"}}),
                     ok({"items": [{"table_id": "source", "name": "2026年报关数据"}, {"table_id": "fx", "name": "汇率"}]}),
                     ok({"items": [], "has_more": False}),
                     ok({"items": [item(records[0])], "has_more": True, "page_token": "next"}),
                     {"code": 1}]
        with patch.object(svc, "ROOT", self.path), patch.object(svc, "OFFLINE", False), \
             patch.object(svc.feishu, "tenant_token", return_value="mock"), \
             patch.object(svc.feishu, "table_target", return_value=("app", "source")):
            with patch.object(svc.feishu, "_request", side_effect=responses):
                with self.assertRaises(svc.feishu.FeishuError):
                    svc.query_job(task)
            self.assertEqual(task["checkpoint"]["pending_search"]["cursor"], "next")
            resume_responses = [
                ok({"items": [item(records[1])], "has_more": False}),
                ok({"items": [], "has_more": False}),
            ]
            with patch.object(svc.feishu, "_request", side_effect=resume_responses) as request:
                result = svc.query_job(task)
                self.assertEqual(request.call_count, 2)
                self.assertIn("page_token=next", request.call_args_list[0].args[0])
        with store.connect() as db:
            self.assertEqual(store.get(db, "snapshot", result["snapshot_id"])["matched"], 2)

    def test_full_snapshot_regression(self):
        source, _ = legacy.apply_add_inheritance(copy.deepcopy(self.source))
        old_success, new_success, rounding_differences = 0, 0, 0
        blocked_customers = 0
        for group in legacy.group_records(source).values():
            rows, issues = engine.build(group, self.masters, self.catalogs)
            draft = dict(self.sample, rows=rows, build_issues=issues)
            new_success += int(engine.validate(draft, self.catalogs)["valid"])
            if not self.masters.customer_for_records(group):
                blocked_customers += 1
                self.assertFalse(draft["valid"])
                self.assertTrue(any(i.get("field") == "客户编码" for i in draft["issues"]))
            try:
                old = legacy.build_voucher(group, self.masters)
            except legacy.VoucherError:
                continue
            old_success += 1
            self.assertTrue(draft["valid"], draft["issues"])
            self.assertEqual(len(old), len(rows))
            for a, b in zip(old, rows):
                for field in ("科目编码", "客户编码", "供应商编码", "存货档案"):
                    self.assertEqual(a[field], b[field])
                for field in ("借方金额", "贷方金额", "原币借方", "原币贷方"):
                    diff = abs(engine.number(a[field]) - engine.number(b[field]))
                    self.assertLessEqual(diff, engine.Decimal(".02"), (field, a[field], b[field]))
                    rounding_differences += int(bool(diff))
        # Ambiguous/unverified customers now remain blocked rather than guessed.
        self.assertGreater(old_success + blocked_customers, 450)
        self.assertGreaterEqual(new_success, old_success)

    def test_partial_export_and_stale_version_rejected(self):
        d = copy.deepcopy(self.draft); d["id"] = "invalid"; d["rows"][0]["科目编码"] = ""
        with store.connect(True) as db:
            store.put(db, "draft", d["id"], d)
        plan = svc.export_plan([self.draft["id"], d["id"]])
        self.assertEqual(len(plan["valid"]), 1)
        self.assertEqual(len(plan["excluded"]), 1)
        task = self.task("export", {"drafts": [{"id": self.draft["id"], "version": 0}]})
        with self.assertRaises(HTTPException):
            svc.export_job(task)
        self.assertFalse(svc.OUTPUT.exists())

    def test_export_capacity_boundary_and_concurrent_double_export(self):
        svc.validate_export_capacity([{"rows": [{}] * 65535}])
        with self.assertRaises(HTTPException) as ctx:
            svc.validate_export_capacity([{"rows": [{}] * 65536}])
        self.assertEqual(ctx.exception.status_code, 422)

        def export(task_id):
            try:
                return svc.export_job({
                    "id": task_id,
                    "payload": {"drafts": [{"id": self.draft["id"], "version": 1}]},
                    "session": task_id,
                })
            except HTTPException as error:
                return error.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(export, ("parallel-a", "parallel-b")))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn(409, results)
        with store.connect() as db:
            self.assertEqual(len(store.all_items(db, "export")), 1)

    def test_task_idempotency(self):
        with patch.object(svc, "submit"):
            first = svc.create_task("query", {"mode": "pending"}, "test", "same")
            second = svc.create_task("query", {"mode": "pending"}, "test", "same")
            self.assertEqual(first["id"], second["id"])
            with self.assertRaises(HTTPException):
                svc.create_task("query", {"mode": "date"}, "test", "same")

    def test_mapping_save_disable_and_history(self):
        catalogs = self.catalogs
        with patch.object(svc, "sync_mapping_files"), patch.object(svc, "catalog", return_value=catalogs):
            code = str(catalogs["inventory"][0]["code"])
            state = svc.save_mapping("inventory", {"version": 1, "item": {
                "alias": "测试品名", "platform_code": code, "enabled": True}}, "test")
            item = state["items"]["inventory"][-1]
            self.assertEqual(item["platform_code"], code)
            with store.connect() as db:
                masters, _ = svc.masters_for(db)
            self.assertEqual(masters.inventory_code("测试品名"), code)
            item["enabled"] = False
            svc.save_mapping("inventory", {"version": 2, "item": item}, "test")
            with store.connect() as db:
                masters, _ = svc.masters_for(db)
            self.assertIsNone(masters.inventory_code("测试品名"))
            with self.assertRaises(HTTPException):
                svc.save_mapping("inventory", {"version": 1, "item": item}, "other")

    def test_domestic_customer_mappings_are_persisted_and_deduplicated(self):
        with store.connect() as db:
            state = svc.mapping_state(db)
        rows = state["items"]["customers"]
        by_alias = {
            legacy.normalize_name(row["alias"]): row
            for row in rows
        }
        expected = {
            "上海宝冶集团有限公司": "010251",
            "万里旺(上海)钢管有限公司": "010240",
            "浙江海洲制药股份有限公司": "D-016",
        }
        for alias, code in expected.items():
            row = by_alias[legacy.normalize_name(alias)]
            self.assertEqual(row["platform_code"], code)
            self.assertTrue(row["basis"])
        for row in svc.domestic_customer_mapping_rows():
            self.assertIn(legacy.normalize_name(row["alias"]), by_alias)
        self.assertEqual(len(by_alias), len(rows))
        catalogs = svc.catalog()
        for kind in ("customers", "suppliers", "inventory"):
            codes = {str(row["code"]) for row in catalogs[kind]}
            self.assertTrue(all(row["platform_code"] in codes for row in state["items"][kind]))
        self.assertTrue(all(row.get("group_name") for row in catalogs["inventory"]))
        customer_groups = {
            row["code"]: row.get("group_name")
            for row in catalogs["customers"]
        }
        for code in ("000026", "000062", "D-016"):
            self.assertEqual(customer_groups[code], "国内")
        for code in ("000129-", "000049", "3-104", "3-118"):
            self.assertEqual(customer_groups[code], "国外")

        with store.connect(True) as db:
            persisted = svc.persist_mapping_migrations(db)
        with store.connect() as db:
            reloaded = svc.mapping_state(db)
            self.assertEqual(reloaded, persisted)
            self.assertEqual(svc.mapping_state(db), reloaded)
            masters, _ = svc.masters_for(db)
        # Live financial users may confirm this formerly fitted-only alias.
        confirmed = by_alias.get(legacy.normalize_name("上海宝冶冶金工程有限公司"))
        expected_code = (confirmed["platform_code"] if confirmed and confirmed.get("enabled", True)
                         and confirmed.get("basis") != legacy.AUTO_CUSTOMER_BASIS else None)
        self.assertEqual(masters.customer_code(["上海宝冶冶金工程有限公司"]), expected_code)
        self.assertEqual(masters.customer_code(["上海辉懿机械有限公司"]), "010243")

        self.assertEqual(
            domestic.customer_for_records(
                [{"销售方式": "内销", "客户名称_脚本用": "上海辉懿机械有限公司"}], masters
            ),
            "010243",
        )

    def test_delete_generation_record_removes_unshared_drafts_and_keeps_export_history(self):
        other = {**copy.deepcopy(self.draft), "id": "other-draft", "version": 1,
                 "contracts": ["26MT-DELETE-OTHER"]}
        with store.connect(True) as db:
            store.put(db, "draft", other["id"], other)
            store.put(db, "batch", "batch-delete", {
                "id": "batch-delete", "created_at": store.now(),
                "draft_ids": [self.draft["id"], other["id"]],
            })
            store.put(db, "batch", "batch-shared", {
                "id": "batch-shared", "created_at": store.now(),
                "draft_ids": [other["id"]],
            })
            store.put(db, "export", "export-history", {
                "id": "export-history", "filename": "history.xls",
                "drafts": [copy.deepcopy(self.draft)], "record_ids": ["source"],
                "updates": [], "writeback": "done", "written": 0,
            })
        result = svc.delete_batch("batch-delete", "test")
        self.assertEqual(result["deleted"], "batch-delete")
        self.assertEqual(result["deleted_drafts"], [self.draft["id"]])
        with store.connect() as db:
            self.assertIsNone(store.get(db, "draft", self.draft["id"]))
            self.assertIsNotNone(store.get(db, "draft", other["id"]))
            self.assertIsNone(store.get(db, "batch", "batch-delete"))
            self.assertIsNotNone(store.get(db, "batch", "batch-shared"))
            self.assertIsNotNone(store.get(db, "export", "export-history"))
            self.assertTrue(any(a["entity"] == self.draft["id"] and a["action"] == "delete"
                                for a in store.all_items(db, "audit")))


class HTTPFlowTests(unittest.TestCase):
    """Real HTTP boundary and worker integration, isolated from production data."""
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cls.base = f"http://127.0.0.1:{cls.port}/api"
        env = dict(os.environ, WORKBENCH_OFFLINE="1",
                   WORKBENCH_DB=str(Path(cls.temp.name) / "http.sqlite3"),
                   WORKBENCH_OUTPUT=str(Path(cls.temp.name) / "exports"))
        cls.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=svc.ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        for _ in range(100):
            try:
                cls.request("/meta")
                return
            except OSError:
                time.sleep(.1)
        cls.process.terminate(); cls.process.wait(); cls.temp.cleanup()
        raise RuntimeError("Test HTTP server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=10)
        cls.temp.cleanup()

    @classmethod
    def request(cls, path, payload=None, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(cls.base + path, data=data, method=method,
                          headers={"Content-Type": "application/json", "X-Session-ID": "http-test"})
        with urlopen(request, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if response.headers.get_content_type() == "application/json" else raw

    @classmethod
    def request_error(cls, path, payload=None, method="GET", api=True):
        data = json.dumps(payload).encode() if payload is not None else None
        base = cls.base if api else cls.base[:-4]
        request = Request(base + path, data=data, method=method,
                          headers={"Content-Type": "application/json", "X-Session-ID": "http-test"})
        try:
            urlopen(request, timeout=15)
        except HTTPError as error:
            raw = error.read()
            body = json.loads(raw) if error.headers.get_content_type() == "application/json" else raw
            return error.code, body
        raise AssertionError("request unexpectedly succeeded")

    def job(self, kind, payload):
        task = self.request("/tasks", {"kind": kind, "payload": payload, "request_id": str(time.time_ns())})
        for _ in range(100):
            task = self.request("/tasks/" + task["id"])
            if task["status"] in ("done", "failed"):
                self.assertEqual(task["status"], "done", task)
                return task["result"]
            time.sleep(.05)
        self.fail("Task timed out")

    def test_query_build_edit_export_download(self):
        self.assertTrue(self.request("/meta")["offline"])
        result = self.job("query", {"mode": "pending"})
        snapshot = self.request("/snapshots/" + result["snapshot_id"])
        selected, draft = None, None
        for candidate in (g for g in snapshot["groups"] if not g["blocked"]):
            result = self.job("build", {"snapshot_id": snapshot["id"], "keys": [candidate["key"]], "posting_date": "2026-10-20"})
            batch = self.request("/batches/" + result["batch_id"])
            candidate_draft = batch["drafts"][0]
            if candidate_draft["valid"]:
                selected, draft = candidate, candidate_draft
                break
        self.assertIsNotNone(selected)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["header"]["maker"], settings.voucher_maker())
        numeric_rows = copy.deepcopy(draft["rows"])
        for row in numeric_rows:
            for field in engine.NUMBERS:
                value = row.get(field)
                if isinstance(value, str) and value.strip():
                    try:
                        row[field] = float(value)
                    except ValueError:
                        pass
        saved = self.request(
            "/drafts/" + draft["id"],
            {**{k: draft[k] for k in ("version", "header", "overrides")}, "rows": numeric_rows},
            "PATCH",
        )
        self.assertTrue(all(isinstance(value, str) for row in saved["rows"] for value in row.values()))
        self.assertTrue(saved["valid"], saved["issues"])
        self.assertEqual(saved["manual_fields"], [])
        draft = saved
        draft["rows"][0]["摘要"] += "（本地测试）"
        saved = self.request("/drafts/" + draft["id"], {k: draft[k] for k in ("version", "header", "rows", "overrides")}, "PATCH")
        self.assertEqual(saved["version"], draft["version"] + 1)
        plan = self.request("/exports/preview", {"draft_ids": [draft["id"]]})
        self.assertEqual(len(plan["valid"]), 1)
        result = self.job("export", {"drafts": [{"id": draft["id"], "version": saved["version"]}]})
        export = self.request("/exports/" + result["export_id"])
        self.assertEqual(export["writeback"], "offline")
        excel = self.request("/exports/" + export["id"] + "/download")
        book = xlrd.open_workbook(file_contents=excel)
        self.assertEqual(book.sheet_by_index(0).ncols, 53)
        self.assertEqual(len(self.request("/history/" + draft["id"])), 3)
        # New query excludes locally occupied records even before any Feishu writeback.
        again = self.job("query", {"mode": "pending"})
        refreshed = self.request("/snapshots/" + again["snapshot_id"])
        self.assertNotIn(selected["key"], [g["key"] for g in refreshed["groups"]])

    def test_retired_and_invalid_http_requests_are_explicit(self):
        status, body = self.request_error("/tasks", {
            "kind": "query",
            "payload": {"mode": "date", "start_date": "2026-01-01", "end_date": "2026-12-31"},
            "request_id": "http-retired-date",
        }, "POST")
        self.assertEqual(status, 422)
        self.assertIn("已停用", body["detail"])
        status, body = self.request_error("/vouchers/generate-bulk", {}, "POST", api=False)
        self.assertEqual(status, 410)
        self.assertIn("已停用", body["detail"])
        status, _ = self.request_error(
            "/vouchers/groups?sale_method=%E5%86%85%E9%94%80", api=False)
        self.assertEqual(status, 422)
        status, _ = self.request_error("/history/not-found?limit=0")
        self.assertEqual(status, 422)
        for path in ("/.env", "/data/ai_customs_source.json", "/app/main.py",
                     "/..%2F.env"):
            status, _ = self.request_error(path, api=False)
            self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
