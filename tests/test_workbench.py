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
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import xlrd
from fastapi import HTTPException

from app.services import draft_service as engine
from app.services import voucher_service as legacy
from app.services import workbench_service as svc
from app.services import workbench_store as store


class WorkbenchTests(unittest.TestCase):
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
        cls.source = [dict(r["fields"], record_id=r["record_id"]) for r in raw if svc.in_scope(r["fields"])]
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

    def test_group_completion_conflicts_and_date_boundaries(self):
        base = copy.deepcopy(self.draft["source"][0])
        base.update(record_id="a", 报关单号="group", 是否开凭证="待开",
                    出口日期=dt.datetime(2026, 9, 30, 23, 59, 59, tzinfo=store.TZ).timestamp() * 1000)
        second = dict(base, record_id="b", 是否开凭证="", 出口日期=dt.datetime(2026, 10, 1, tzinfo=store.TZ).timestamp() * 1000)
        task = self.task("query", {"mode": "date", "start_date": "2026-09-30", "end_date": "2026-09-30"},
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

    def test_contract_search_joint_and_add(self):
        text = engine.contract_index(["PI-26MT-01S180&190&241", "26MT-06H251&ADD1", "26MT-09T039Y-2-鸿迪"])
        for expected in ("26MT-01S190", "26MT-01S241", "26MT-06H251-ADD1", "26MT09T039Y2鸿迪"):
            self.assertIn(expected, text)

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
        task = self.task("query", {"mode": "date", "start_date": "2020-01-01", "end_date": "2030-12-31"})
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
            self.assertEqual(task["checkpoint"]["cursor"], "next")
            with patch.object(svc.feishu, "_request", return_value=ok({"items": [item(records[1])], "has_more": False})) as request:
                result = svc.query_job(task)
                self.assertEqual(request.call_count, 1)
                self.assertIn("page_token=next", request.call_args.args[0])
        with store.connect() as db:
            self.assertEqual(store.get(db, "snapshot", result["snapshot_id"])["matched"], 2)

    def test_full_snapshot_regression(self):
        source, _ = legacy.apply_add_inheritance(copy.deepcopy(self.source))
        old_success, new_success, rounding_differences = 0, 0, 0
        for group in legacy.group_records(source).values():
            rows, issues = engine.build(group, self.masters, self.catalogs)
            draft = dict(self.sample, rows=rows, build_issues=issues)
            new_success += int(engine.validate(draft, self.catalogs)["valid"])
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
        self.assertGreater(old_success, 450)
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
        result = self.job("query", {"mode": "date", "start_date": "2026-01-01", "end_date": "2026-10-20"})
        snapshot = self.request("/snapshots/" + result["snapshot_id"])
        selected = next(g for g in snapshot["groups"] if not g["blocked"])
        result = self.job("build", {"snapshot_id": snapshot["id"], "keys": [selected["key"]], "posting_date": "2026-10-20"})
        batch = self.request("/batches/" + result["batch_id"])
        draft = batch["drafts"][0]
        self.assertTrue(draft["valid"], draft["issues"])
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
        self.assertEqual(len(self.request("/history/" + draft["id"])), 2)
        # New query excludes locally occupied records even before any Feishu writeback.
        again = self.job("query", {"mode": "date", "start_date": "2026-01-01", "end_date": "2026-10-20"})
        refreshed = self.request("/snapshots/" + again["snapshot_id"])
        self.assertNotIn(selected["key"], [g["key"] for g in refreshed["groups"]])


if __name__ == "__main__":
    unittest.main()
