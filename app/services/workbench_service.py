"""Persistent workbench orchestration, versioned edits and recoverable jobs."""
from __future__ import annotations

import copy
import calendar
import datetime as dt
import hashlib
import json
import logging
import os
import threading
import uuid
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

from fastapi import HTTPException

from . import draft_service as engine
from . import feishu_service as feishu
from . import voucher_service as legacy
from . import workbench_store as store

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(os.environ.get("WORKBENCH_OUTPUT", ROOT / "outputs/workbench"))
OFFLINE = os.environ.get("WORKBENCH_OFFLINE") == "1"
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workbench")
LOCK = threading.RLock()
ACTIVE = set()
MAP_FILES = {"customers": ("customer_alias_mappings.json", "aliases"),
             "suppliers": ("finance_supplier_alias_mappings.json", "mappings"),
             "inventory": ("product_alias_mappings.json", "mappings")}
CATALOG_FILES = {"customers": "客户档案", "suppliers": "供应商档案",
                 "inventory": "存货档案", "subjects": "会计科目"}

@lru_cache(maxsize=1)
def initial_source_label():
    path = ROOT / "data/ai_customs_source.json"
    if path.exists():
        data = legacy.read_json(path)
        return str(data.get("app_name") or "2025海关 AI") + " / 2026年报关数据"
    return "2025海关 AI / 2026年报关数据"


def fail(status, message):
    raise HTTPException(status, message)


def required(db, kind, key):
    obj = store.get(db, kind, key)
    if obj is None:
        fail(404, "未找到记录")
    return obj


def workbench_batches(db, now=None):
    moment = now or dt.datetime.now(store.TZ)
    year, month = (moment.year, moment.month - 1) if moment.month > 1 else (moment.year - 1, 12)
    cutoff = moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))
    def recent(value):
        try:
            stamp = dt.datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=store.TZ)
            return cutoff <= stamp <= moment
        except (ValueError, TypeError):
            return False
    drafts = {d["id"]: d for d in store.all_items(db, "draft")}
    result = []
    for batch in store.all_items(db, "batch"):
        if not recent(batch.get("created_at")):
            continue
        keys = [key for key in batch["draft_ids"] if key in drafts
                and not drafts[key].get("exports")
                and recent(drafts[key].get("created_at", batch["created_at"]))]
        if keys:
            result.append({**batch, "draft_ids": keys})
    return sorted(result, key=lambda b: b["created_at"], reverse=True)[:20]


def catalog():
    result = {}
    for kind, filename in CATALOG_FILES.items():
        records = legacy.read_json(ROOT / "mappings/platform" / (filename + ".json"))["records"]
        by_id = {str(r["id"]): r for r in records}
        for record in records:
            parent = by_id.get(str(record.get("parent", "")))
            path = str(record.get("path", "")).strip("|").split("|")
            if not parent and len(path) > 1:
                parent = by_id.get(path[-2])
            if parent:
                record["group_name"] = str(parent["code"]) + " · " + parent["name"]
            elif kind == "customers" and record.get("customerClass_name"):
                record["group_name"] = record["customerClass_name"]
        if kind == "inventory":
            records = [r for r in records if r.get("isEnd")]
            for record in records:
                category = legacy.PRODUCT_CODE_MAPPING.get(str(record["code"])[:4], "其他产品")
                record["subject_code"] = legacy.INVENTORY_SUBJECT[category]
                record["subject_name"] = category
        result[kind] = records
        result[kind + "_by_code"] = {str(r["code"]): r for r in records}
    aux = legacy.read_json(ROOT / "mappings/platform/科目辅助核算配置.json")["records"]
    result["aux"] = aux
    result["aux_by_code"] = {r["account_code"]: r["auxiliary_fields"] for r in aux}
    rates = ROOT / "data/fx_rates_ai_snapshot.json"
    result["rates"] = legacy.read_json(rates).get("records", []) if rates.exists() else []
    with store.connect() as db:
        edits = store.get(db, "settings", "catalog_edits") or {"version": 1, "items": {}}
    for kind, items in edits["items"].items():
        for key, patch in items.items():
            row = next((r for r in result[kind] if catalog_key(kind, r) == key), None)
            if row is not None:
                row.update(copy.deepcopy(patch))
            elif kind == "rates":
                result[kind].append(copy.deepcopy(patch))
    for kind in CATALOG_FILES:
        result[kind + "_by_code"] = {str(r["code"]): r for r in result[kind]}
    result["aux_by_code"] = {r["account_code"]: r["auxiliary_fields"] for r in result["aux"]}
    for row in result["aux"]:
        subject = result["subjects_by_code"].get(row["account_code"], {})
        row["group_name"] = subject.get("group_name", "")
        row["account_name"] = subject.get("displayname", row["account_name"])
    return result


def catalog_key(kind, row):
    return str(row.get("record_id") or row.get("id")) if kind == "rates" else str(row.get("account_code") if kind == "aux" else row.get("code"))


def save_catalog(kind, payload, session):
    if kind not in ("subjects", "aux", "rates"):
        fail(422, "请使用映射编辑接口")
    catalogs = catalog()
    key = str(payload.get("key") or "")
    old = next((r for r in catalogs[kind] if catalog_key(kind, r) == key), None)
    item = payload.get("item", {})
    if not isinstance(item, dict):
        fail(422, "无效记录")
    if kind == "rates":
        fields = item.get("fields", {})
        try:
            year, month = int(fields["实际年份"]), int(fields["实际月份"])
            currency = str(fields["币种"]).strip().upper()
            value = engine.number(fields["汇率"])
            if not 1900 <= year <= 2200 or not 1 <= month <= 12 or not currency.isascii() or not currency.isalpha() or len(currency) != 3 or value is None or value <= 0:
                raise ValueError()
            if currency in ("CNY", "RMB") and value != 1:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            fail(422, "请填写有效年月、三位币种和大于零的汇率")
        if old:
            original = old["fields"]
            curr = original["币种"]
            curr = curr[0] if isinstance(curr, list) else curr
            if (year, month, currency) != (int(original["实际年份"]), int(original["实际月份"]), curr):
                fail(422, "已有汇率的年月和币种不可更改，请新增记录")
        else:
            for r in catalogs["rates"]:
                f = r["fields"]; curr = f.get("币种")
                curr = curr[0] if isinstance(curr, list) and curr else curr
                if (str(f.get("实际年份")), str(f.get("实际月份")).zfill(2), curr) == (str(year), str(month).zfill(2), currency):
                    fail(422, "该年月币种已有记录，请编辑原记录")
            key = "local-" + uuid.uuid4().hex
        patch = {**(old or {}), "id": key, "record_id": key,
                 "fields": {**((old or {}).get("fields", {})), "实际年份": str(year), "实际月份": str(month),
                            "币种": [currency], "汇率": str(value), "数据来源": str(fields.get("数据来源", "本地维护"))}}
    elif not old:
        fail(404, "未找到档案")
    elif kind == "subjects":
        name = str(item.get("name", "")).strip()
        if not name or item.get("direction") not in ("Debit", "Credit"):
            fail(422, "请填写科目名称和借贷方向")
        prefix = old.get("displayname", "").rsplit("_", 1)[0] if "_" in old.get("displayname", "") else ""
        patch = {"name": name, "displayname": prefix + "_" + name if prefix else name, "direction": item["direction"]}
    else:
        fields = item.get("auxiliary_fields")
        templates = {f["code"]: f for r in catalogs["aux"] for f in r["auxiliary_fields"]}
        if not isinstance(fields, list) or any(not isinstance(f, dict) or f.get("code") not in templates or not isinstance(f.get("mustinput"), bool) for f in fields):
            fail(422, "请选择有效辅助项及必填设置")
        if len({f["code"] for f in fields}) != len(fields):
            fail(422, "辅助项不能重复")
        patch = {"auxiliary_fields": [{**templates[f["code"]], "mustinput": f["mustinput"]} for f in fields]}
    with LOCK, store.connect(True) as db:
        state = store.get(db, "settings", "catalog_edits") or {"version": 1, "items": {}}
        if payload.get("version") != state["version"]:
            fail(409, "数据源已被修改，请刷新后重试；当前输入已保留")
        state["items"].setdefault(kind, {})[key] = patch
        state["version"] += 1
        store.put(db, "settings", "catalog_edits", state)
        mappings = mapping_state(db)
        mappings["version"] += 1
        store.put(db, "settings", "mappings", mappings)
        store.audit(db, "catalog:" + kind + ":" + key, session, "save_catalog", old, patch)
    legacy.MASTERS = None
    return {"version": state["version"]}


def mapping_state(db):
    state = store.get(db, "settings", "mappings")
    if state:
        return state
    state = {"version": 1, "items": {}}
    for kind, (filename, key) in MAP_FILES.items():
        path = ROOT / "mappings" / filename
        payload = legacy.read_json(path) if path.exists() else {key: []}
        state["items"][kind] = [{**r, "enabled": r.get("enabled", True),
                                "id": str(i), "updated_at": r.get("updated_at", "")}
                               for i, r in enumerate(payload[key])]
        if kind == "inventory" and not path.exists():
            state["items"][kind] = [
                {"id": "default-" + hashlib.sha256(name.encode()).hexdigest()[:16],
                 "alias": name, "platform_code": code, "platform_name": name,
                 "enabled": True, "updated_at": "", "basis": "同名存货档案"}
                for name, code in legacy.MasterData._load_inventory().items()]
    return state


def masters_for(db):
    masters = legacy.MasterData()
    state = mapping_state(db)
    for kind, rows in state["items"].items():
        for row in rows:
            if not row.get("enabled", True):
                if kind == "customers":
                    masters.customer_aliases.pop(legacy.normalize_name(row["alias"]), None)
                elif kind == "suppliers":
                    masters.supplier_aliases.pop(row["alias"], None)
                elif kind == "inventory":
                    masters.inventory.pop(row["alias"], None)
                continue
            if kind == "customers":
                masters.customer_aliases[legacy.normalize_name(row["alias"])] = row["platform_code"]
            elif kind == "suppliers":
                masters.supplier_aliases[row["alias"]] = row["platform_code"]
            else:
                masters.inventory[row["alias"]] = row["platform_code"]
    edits = store.get(db, "settings", "catalog_edits") or {"items": {}}
    for row in edits["items"].get("rates", {}).values():
        f = row["fields"]
        masters.rates[(f["实际年份"], f["实际月份"], f["币种"][0])] = engine.number(f["汇率"])
    return masters, state["version"]


def sync_mapping_files(state):
    """Atomic compatibility mirrors; DB is authoritative and repairs mirrors at startup."""
    for kind, (filename, key) in MAP_FILES.items():
        path = ROOT / "mappings" / filename
        payload = legacy.read_json(path) if path.exists() else {key: []}
        payload[key] = [r for r in state["items"][kind] if kind == "inventory" or r.get("enabled", True)]
        payload["updated_at"] = store.now()
        temp = path.with_suffix(".json.tmp")
        temp.write_text(store.dumps(payload), encoding="utf-8")
        temp.replace(path)
    legacy.MASTERS = None


def save_mapping(kind, payload, session):
    if kind not in MAP_FILES:
        fail(422, "未知映射类型")
    catalogs = catalog()
    with LOCK, store.connect(True) as db:
        state = mapping_state(db)
        if payload.get("version") != state["version"]:
            fail(409, "映射已被其他窗口修改，请刷新后重试")
        rows = state["items"][kind]
        incoming = payload["item"]
        alias, code = str(incoming.get("alias", "")).strip(), incoming.get("platform_code")
        if not alias or not isinstance(code, str) or code not in catalogs[kind + "_by_code"]:
            fail(422, "请填写来源名称，并选择有效档案编码")
        key = incoming.get("id") or uuid.uuid4().hex
        if any(r["id"] != key and legacy.normalize_name(r["alias"]) == legacy.normalize_name(alias)
               for r in rows):
            fail(422, "该来源名称已有映射，请编辑已有记录")
        old = next((r for r in rows if r["id"] == key), None)
        item = {**(old or {}), "id": key, "alias": alias, "platform_code": code,
                "platform_name": catalogs[kind + "_by_code"][code]["name"],
                "enabled": bool(incoming.get("enabled", True)), "updated_at": store.now()}
        state["items"][kind] = [r for r in rows if r["id"] != key] + [item]
        state["version"] += 1
        store.put(db, "settings", "mappings", state)
        store.audit(db, "mapping:" + kind + ":" + key, session, "save_mapping", old, item)
    if not OFFLINE:
        with LOCK:
            sync_mapping_files(state)
    return state


def task_view(task):
    return {k: task.get(k) for k in ("id", "kind", "status", "progress", "total", "message",
                                    "result", "created_at", "updated_at")}


def create_task(kind, payload, session, key):
    if kind not in ("query", "build", "export", "writeback"):
        fail(422, "未知任务")
    if not key or len(key) > 150:
        fail(422, "缺少请求标识")
    tid = hashlib.sha256((kind + ":" + key).encode()).hexdigest()[:32]
    with store.connect(True) as db:
        old = store.get(db, "task", tid)
        if old:
            if old["payload"] != payload:
                fail(409, "同一请求标识不能用于不同参数")
            return task_view(old)
        task = {"id": tid, "kind": kind, "payload": payload, "session": session,
                "status": "queued", "progress": 0, "total": 0, "created_at": store.now(),
                "updated_at": store.now(), "checkpoint": {}}
        store.put(db, "task", tid, task)
    submit(tid)
    return task_view(task)


def checkpoint(task, **changes):
    task.update(changes, updated_at=store.now())
    with store.connect(True) as db:
        store.put(db, "task", task["id"], task)


def submit(tid):
    with LOCK:
        if tid in ACTIVE:
            return
        ACTIVE.add(tid)
    POOL.submit(run, tid)


def run(tid):
    with store.connect() as db:
        task = required(db, "task", tid)
    try:
        checkpoint(task, status="running", message="处理中")
        result = {"query": query_job, "build": build_job, "export": export_job,
                  "writeback": writeback_job}[task["kind"]](task)
        checkpoint(task, status="done", result=result, message="完成")
    except Exception as exc:
        logging.exception("Workbench task failed: %s", tid)
        message = exc.detail if isinstance(exc, HTTPException) else (
            str(exc) if isinstance(exc, (feishu.FeishuError, ValueError)) else "处理失败，可重试；详细原因已记录在服务日志")
        checkpoint(task, status="failed", message=message)
    finally:
        with LOCK:
            ACTIVE.discard(tid)


def resume():
    with store.connect() as db:
        tasks = store.all_items(db, "task")
        state = store.get(db, "settings", "mappings")
    if state and not OFFLINE:
        sync_mapping_files(state)
    for task in reversed(tasks):
        if task["status"] in ("queued", "running"):
            submit(task["id"])


def in_scope(r):
    return legacy.to_text(r.get("合同号_1")).upper().removeprefix("PI-").startswith("26MT") and r.get("销售方式") == "出口"


def query_job(task):
    payload, cp = task["payload"], task["checkpoint"]
    mode = payload.get("mode", "pending")
    if mode not in ("pending", "date"):
        fail(422, "请选择待开或日期模式")
    start, end = None, None
    if mode == "date":
        start, end = dt.date.fromisoformat(payload["start_date"]), dt.date.fromisoformat(payload["end_date"])
        if start > end:
            fail(422, "开始日期不能晚于结束日期")
    records = cp.get("records", [])
    if not cp.get("fetched"):
        if OFFLINE:
            raw = legacy.read_json(ROOT / "data/ai_customs_source.json")["records"]
            records = [dict(r["fields"], record_id=r["record_id"]) for r in raw]
        else:
            token = feishu.tenant_token()
            app_token, table_id = feishu.table_target()
            base = f"{feishu.BASE}/bitable/v1/apps/{app_token}"
            if not cp.get("verified"):
                app = feishu._request(base, token=token)
                if app.get("code") != 0:
                    raise feishu.FeishuError("无法核验飞书数据源")
                actual_name = (app.get("data", {}).get("app") or {}).get("name", "")
                expected_name = os.environ.get("WORKBENCH_EXPECTED_APP_NAME")
                accepted_names = {expected_name.replace(" ", "")} if expected_name else {"2025海关AI", "2026年海关AI"}
                if actual_name.replace(" ", "") not in accepted_names:
                    raise feishu.FeishuError("配置的数据源名称不符合预期，请核对后台配置")
                tables, page = [], None
                while True:
                    params = {"page_size": 100}
                    if page:
                        params["page_token"] = page
                    response = feishu._request(base + "/tables?" + urlencode(params), token=token)
                    if response.get("code") != 0:
                        raise feishu.FeishuError("无法核验飞书数据表")
                    data = response.get("data", {})
                    tables += data.get("items", [])
                    if not data.get("has_more"):
                        break
                    page = data.get("page_token")
                    if not page:
                        raise feishu.FeishuError("飞书表目录分页信息不完整")
                table = next((t for t in tables if t["table_id"] == table_id), None)
                if not table or table["name"] != "2026年报关数据":
                    raise feishu.FeishuError("配置的表不是2026年报关数据，请核对后台配置")
                rate_table = next((t["table_id"] for t in tables if t["name"] == "汇率"), None)
                if not rate_table:
                    raise feishu.FeishuError("数据源中未找到汇率表")
                cp.update(verified=True, rate_table=rate_table, actual_name=actual_name)
                checkpoint(task)
            if not cp.get("rates_fetched"):
                rates, rate_cursor = cp.get("rates", []), cp.get("rate_cursor")
                while True:
                    params = {"page_size": 500}
                    if rate_cursor:
                        params["page_token"] = rate_cursor
                    response = feishu._request(base + f"/tables/{cp['rate_table']}/records?" +
                                              urlencode(params), token=token)
                    if response.get("code") != 0:
                        raise feishu.FeishuError("读取飞书汇率失败，请重试")
                    data = response.get("data", {})
                    rates += data.get("items", [])
                    rate_cursor = data.get("page_token")
                    cp.update(rates=rates, rate_cursor=rate_cursor, rates_fetched=not data.get("has_more"))
                    checkpoint(task)
                    if cp["rates_fetched"]:
                        break
                    if not rate_cursor:
                        raise feishu.FeishuError("汇率分页信息不完整")
            rate_path = ROOT / "data/fx_rates_ai_snapshot.json"
            temporary = rate_path.with_suffix(".json.tmp")
            temporary.write_text(store.dumps({"records": cp["rates"], "fetched_at": store.now()}), encoding="utf-8")
            temporary.replace(rate_path)
            legacy.MASTERS = None
            url = f"{feishu.BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
            cursor = cp.get("cursor")
            while True:
                params = {"page_size": 500}
                if cursor:
                    params["page_token"] = cursor
                body = feishu._request(url + "?" + urlencode(params), token=token)
                if body.get("code") != 0:
                    raise feishu.FeishuError("读取飞书记录失败，请重试")
                data = body.get("data") or {}
                records += [dict(r.get("fields", {}), record_id=r["record_id"]) for r in data.get("items", [])]
                cursor = data.get("page_token")
                cp.update(records=records, cursor=cursor, fetched=not data.get("has_more"))
                checkpoint(task, progress=len(records))
                if cp["fetched"]:
                    break
                if not cursor:
                    raise feishu.FeishuError("飞书分页信息不完整")
        cp.update(records=records, fetched=True)
        checkpoint(task, progress=len(records))
    # Deduplicate pages on service restart and preserve full declaration groups.
    records = list({r["record_id"]: r for r in records}.values())
    groups, selected = {}, set()
    with store.connect() as db:
        occupied = {k for e in store.all_items(db, "export") for k in e["record_ids"]}
    excluded, dateless = 0, 0
    for r in records:
        customs = legacy.to_text(r.get("报关单号"))
        key = customs or "missing:" + r["record_id"]
        groups.setdefault(key, []).append(r)
        if not in_scope(r):
            continue
        if legacy.to_text(r.get("是否开凭证")) == "已开" or r["record_id"] in occupied:
            excluded += 1
            continue
        if mode == "pending":
            hit = legacy.to_text(r.get("是否开凭证")) == "待开"
        else:
            stamp = engine.number(r.get("出口日期"))
            if stamp is None:
                dateless += 1
                continue
            day = dt.datetime.fromtimestamp(float(stamp) / 1000, store.TZ).date()
            hit = start <= day <= end
        if hit:
            selected.add(r["record_id"])
    result = []
    for key, group in groups.items():
        hits = [r for r in group if r["record_id"] in selected]
        if not hits:
            continue
        reasons = []
        if key.startswith("missing:"):
            reasons.append("缺少报关单号，请在飞书补充后重新检索")
        if any(r["record_id"] in occupied or legacy.to_text(r.get("是否开凭证")) == "已开" for r in group):
            reasons.append("同一报关单中已有已开或已导出记录")
        if any(not in_scope(r) for r in group):
            reasons.append("同一报关单包含范围外记录")
        statuses = {legacy.to_text(r.get("是否开凭证")) for r in group}
        if any(s not in ("", "待开", "未开", "未开凭证") for s in statuses):
            reasons.append("同一报关单存在需核实的凭证状态")
        contracts = list(dict.fromkeys(legacy.to_text(r.get("合同号_1")) for r in group))
        result.append({"key": key, "customs_no": "" if key.startswith("missing:") else key,
                       "contracts": contracts, "search_index": engine.contract_index(contracts),
                       "count": len(group), "matched": len(hits), "added": len(group) - len(hits),
                       "blocked": reasons, "records": group})
    source_label = cp.get("actual_name", "") + " / 2026年报关数据" if cp.get("actual_name") else initial_source_label()
    snapshot = {"id": task["id"], "created_at": store.now(), "mode": mode, "groups": result,
                "matched": len(selected), "excluded": excluded, "dateless": dateless,
                "source": source_label, "offline": OFFLINE,
                "all_records": records}
    with store.connect(True) as db:
        store.put(db, "snapshot", snapshot["id"], snapshot)
        store.put(db, "settings", "source", {"label": source_label, "updated_at": store.now()})
    return {"snapshot_id": snapshot["id"], "groups": len(result), "matched": len(selected)}


def build_job(task):
    p = task["payload"]
    dt.date.fromisoformat(p["posting_date"])
    with store.connect() as db:
        snap = required(db, "snapshot", p["snapshot_id"])
        masters, version = masters_for(db)
    keys = set(p["keys"])
    groups = [g for g in snap["groups"] if g["key"] in keys]
    if not groups or len(groups) != len(keys):
        fail(422, "请选择有效凭证范围")
    if any(g["blocked"] for g in groups):
        fail(422, "所选范围存在分组冲突，请核实后重新检索")
    catalogs = catalog()
    inherited, notes = legacy.apply_add_inheritance(copy.deepcopy(snap["all_records"]))
    by_id = {r["record_id"]: r for r in inherited}
    batch = {"id": task["id"], "created_at": task["created_at"], "draft_ids": [],
             "snapshot_id": snap["id"], "mode": snap["mode"]}
    for i, group in enumerate(groups):
        key = hashlib.sha256(group["key"].encode()).hexdigest()[:24]
        with store.connect(True) as db:
            draft = store.get(db, "draft", key)
            if draft is None:
                source = copy.deepcopy(group["records"])
                rows, issues = engine.build([by_id[r["record_id"]] for r in source], masters, catalogs)
                draft = {"id": key, "customs_no": group["customs_no"], "contracts": group["contracts"],
                         "search_index": group["search_index"], "source": source, "overrides": {},
                         "effective_source": [by_id[r["record_id"]] for r in source],
                         "header": {"posting_date": p["posting_date"], "category": "记", "maker": "王益"},
                         "rows": rows, "build_issues": issues, "blocking_issues": [],
                         "manual_fields": [], "version": 1, "mapping_version": version,
                         "updated_at": store.now(), "created_at": store.now(), "exports": [],
                         "inherited": [n for n in notes if n["报关单号"] == group["customs_no"]]}
                engine.validate(draft, catalogs)
                store.put(db, "draft", key, draft)
                store.audit(db, key, task["session"], "build", None, draft)
            elif draft["source"] != group["records"]:
                draft["incoming_source"] = group["records"]
                draft["incoming_effective_source"] = [by_id[r["record_id"]] for r in group["records"]]
                draft["version"] += 1
                engine.validate(draft, catalogs)
                store.put(db, "draft", key, draft)
            batch["draft_ids"].append(key)
            store.put(db, "batch", batch["id"], batch)
        checkpoint(task, progress=i + 1, total=len(groups))
    return {"batch_id": batch["id"]}


def save_draft(key, payload, session):
    with store.connect(True) as db:
        old = required(db, "draft", key)
        if payload.get("version") != old["version"]:
            fail(409, "该凭证已被其他窗口修改，本地输入已保留，请比较后重新保存")
        draft = copy.deepcopy(old)
        draft["header"] = payload["header"]
        draft["rows"] = payload["rows"]
        allowed_columns = {"id", "摘要", "科目编码", "币种名称", *engine.NUMBERS, *engine.AUX}
        if any(not r.get("id") or set(r) - allowed_columns for r in draft["rows"]):
            fail(422, "分录缺少标识或包含不支持的字段")
        for row in draft["rows"]:
            for field in [*engine.AUX, *engine.NUMBERS[5:]]:
                row.setdefault(field, "")
        draft["overrides"] = payload.get("overrides", {})
        allowed = set(engine.SOURCE_FIELDS)
        for rid, fields in draft["overrides"].items():
            if rid not in {r["record_id"] for r in draft["source"]} or set(fields) - allowed:
                fail(422, "不允许修改该来源字段")
        prior = {r["id"]: r for r in old["rows"]}
        manual = set(old["manual_fields"])
        if [r["id"] for r in old["rows"]] != [r["id"] for r in draft["rows"]]:
            manual.add("__rows__/structure")
        for row in draft["rows"]:
            for field in row:
                if field != "id" and row.get(field) != prior.get(row["id"], {}).get(field):
                    manual.add(row["id"] + "/" + field)
        draft["manual_fields"] = sorted(manual)
        draft["version"] += 1
        draft["updated_at"] = store.now()
        engine.validate(draft, catalog())
        store.put(db, "draft", key, draft)
        store.audit(db, key, session, "edit", old, draft)
    return draft


def recalculate(key, payload, session):
    with store.connect(True) as db:
        old = required(db, "draft", key)
        if old["version"] != payload["version"]:
            fail(409, "凭证版本已更新，请刷新后重试")
        if old["manual_fields"] and not payload.get("replace_manual"):
            fail(409, "重新计算将覆盖人工分录修改，请明确确认；原版本会保留在修改历史")
        if old.get("incoming_source") and not payload.get("refresh_source"):
            fail(409, "飞书来源已更新，请确认采用最新来源后重新计算")
        source = old.get("incoming_source") or old["source"]
        effective = old.get("incoming_effective_source") or old.get("effective_source") or source
        # Apply explicit overrides after inherited source values.
        records = [dict(r, **old["overrides"].get(r["record_id"], {})) for r in effective]
        masters, version = masters_for(db)
        catalogs = catalog()
        draft = copy.deepcopy(old)
        draft["source"], draft["effective_source"] = source, effective
        draft["overrides"] = {k: v for k, v in draft["overrides"].items()
                              if k in {r["record_id"] for r in source}}
        draft.pop("incoming_source", None)
        draft.pop("incoming_effective_source", None)
        draft["rows"], draft["build_issues"] = engine.build(records, masters, catalogs)
        draft["calculated_overrides"] = copy.deepcopy(draft["overrides"])
        draft["contracts"] = list(dict.fromkeys(legacy.to_text(r.get("合同号_1")) for r in records))
        draft["search_index"] = engine.contract_index(draft["contracts"])
        draft.update(manual_fields=[], mapping_version=version, version=old["version"] + 1, updated_at=store.now())
        engine.validate(draft, catalogs)
        store.put(db, "draft", key, draft)
        store.audit(db, key, session, "recalculate", old, draft)
    return draft


def export_plan(ids):
    valid, excluded = [], []
    with store.connect() as db:
        catalogs = catalog()
        for key in dict.fromkeys(ids):
            draft = engine.validate(required(db, "draft", key), catalogs)
            entry = {"id": key, "contracts": draft["contracts"], "version": draft["version"],
                     "replaces": draft["exports"]}
            if draft["valid"]:
                valid.append(entry)
            else:
                entry["issues"] = draft["issues"]
                excluded.append(entry)
    return {"valid": valid, "excluded": excluded}


def write_excel(drafts, path):
    book, sheet, columns = legacy._new_sheet(path)
    sheet._cell_overwrite_ok = True
    index = 1
    for voucher_id, draft in enumerate(drafts, 1):
        for row in draft["rows"]:
            converted = {k: (float(engine.number(v)) if engine.number(v) is not None else None)
                         if k in engine.NUMBERS else v for k, v in row.items()}
            legacy._write_row(sheet, columns, index, converted, voucher_id,
                              dt.date.fromisoformat(draft["header"]["posting_date"]))
            for field, value in (("制单人", draft["header"]["maker"]), ("凭证类别", draft["header"]["category"])):
                # _write_row already writes these cells; overwrite is enabled on the sheet.
                sheet.write(index, columns[field], value)
            index += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    book.save(str(temporary))
    temporary.replace(path)


def export_job(task):
    eid = task["id"]
    with store.connect(True) as db:
        existing = store.get(db, "export", eid)
        if not existing:
            drafts = []
            catalogs = catalog()
            for item in task["payload"]["drafts"]:
                draft = required(db, "draft", item["id"])
                if draft["version"] != item["version"]:
                    fail(409, "预览后凭证发生变化，请重新预览导出")
                if not engine.validate(draft, catalogs)["valid"]:
                    fail(422, "凭证尚有阻断问题")
                drafts.append(draft)
            if not drafts or len({d["id"] for d in drafts}) != len(drafts):
                fail(422, "导出范围为空或重复")
            if 1 + sum(len(d["rows"]) for d in drafts) > 65536:
                fail(422, "超出xls单表行数限制，请减少导出范围")
            prior = store.all_items(db, "export")
            record_ids = [r["record_id"] for d in drafts for r in d["source"]]
            for existing_export in prior:
                if set(record_ids) & set(existing_export["record_ids"]):
                    if not task["payload"].get("replace_export"):
                        fail(409, "范围已导出，请确认这是替换文件后重试")
            path = OUTPUT / ("用友凭证_" + eid[:12] + ".xls")
            write_excel(drafts, path)
            updates = []
            for draft in drafts:
                for source in draft["source"]:
                    updates.append({"record_id": source["record_id"], "fields": {
                        "是否开凭证": "已开", "凭证开具日期": draft["header"]["posting_date"][:7],
                        "凭证开具结果": "保存成功"}})
            existing = {"id": eid, "created_at": store.now(), "filename": path.name,
                        "drafts": copy.deepcopy(drafts), "record_ids": record_ids, "updates": updates,
                        "writeback": "pending", "written": 0, "offline": OFFLINE, "error": ""}
            store.put(db, "export", eid, existing)
            for draft in drafts:
                draft["exports"].append(eid)
                # Export metadata changes participate in optimistic locking.
                draft["version"] += 1
                store.put(db, "draft", draft["id"], draft)
            store.audit(db, eid, task["session"], "export", None,
                        {"draft_ids": [d["id"] for d in drafts], "filename": path.name})
    checkpoint(task, progress=1, total=2, result={"export_id": eid})
    writeback(eid)
    return {"export_id": eid}


def writeback(eid):
    with store.connect() as db:
        export = required(db, "export", eid)
        ordered = store.all_items(db, "export")
        position = next(i for i, e in enumerate(ordered) if e["id"] == eid)
        newer = [e for e in ordered[:position]
                 if set(e.get("record_ids", [])) & set(export.get("record_ids", []))]
    if export["writeback"] == "done":
        return
    if newer:
        export.update(writeback="superseded", error="此文件已有新版，请重试最新版本的回写")
        with store.connect(True) as db:
            store.put(db, "export", eid, export)
        return
    if export["offline"]:
        export["writeback"] = "offline"
        with store.connect(True) as db:
            store.put(db, "export", eid, export)
        return
    try:
        for start in range(export["written"], len(export["updates"]), 100):
            chunk = export["updates"][start:start + 100]
            feishu.batch_update(chunk)  # Setting the same field values is idempotent after a crash.
            export["written"] = start + len(chunk)
            with store.connect(True) as db:
                store.put(db, "export", eid, export)
        export.update(writeback="done", error="")
    except Exception:
        export.update(writeback="failed", error="飞书回写失败，文件已保留，请重试回写")
        logging.exception("Feishu writeback failed for %s", eid)
    with store.connect(True) as db:
        store.put(db, "export", eid, export)


def writeback_job(task):
    eid = task["payload"]["export_id"]
    writeback(eid)
    return {"export_id": eid}
