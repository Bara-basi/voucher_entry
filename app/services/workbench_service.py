"""Persistent workbench orchestration, versioned edits and recoverable jobs."""
from __future__ import annotations

import copy
import calendar
import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

from fastapi import HTTPException

from . import draft_service as engine
from . import domestic_voucher_service as domestic
from . import feishu_service as feishu
from . import voucher_service as legacy
from . import workbench_store as store

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(os.environ.get("WORKBENCH_OUTPUT", ROOT / "outputs/workbench"))
OFFLINE = os.environ.get("WORKBENCH_OFFLINE") == "1"
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workbench")
LOCK = threading.RLock()
ACTIVE = set()
SOURCE_CONTEXT_CACHE: dict[tuple[str, str], tuple[float, str, str]] = {}
SOURCE_CONTEXT_TTL = 600
RATE_CACHE_TTL = 6 * 60 * 60
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


def verified_source_context(token, app_token, table_id):
    key = (app_token, table_id)
    cached = SOURCE_CONTEXT_CACHE.get(key)
    if cached and cached[0] > time.time():
        return cached[1], cached[2]
    base = f"{feishu.BASE}/bitable/v1/apps/{app_token}"
    app = feishu._request(base, token=token)
    if app.get("code") != 0:
        raise feishu.FeishuError("无法核验飞书数据源")
    actual_name = (app.get("data", {}).get("app") or {}).get("name", "")
    expected_name = os.environ.get("WORKBENCH_EXPECTED_APP_NAME")
    accepted_names = (
        {expected_name.replace(" ", "")}
        if expected_name else {"2025海关AI", "2026年海关AI"}
    )
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
    table = next((item for item in tables if item["table_id"] == table_id), None)
    if not table or table["name"] != "2026年报关数据":
        raise feishu.FeishuError("配置的表不是2026年报关数据，请核对后台配置")
    rate_table = next((item["table_id"] for item in tables if item["name"] == "汇率"), None)
    if not rate_table:
        raise feishu.FeishuError("数据源中未找到汇率表")
    SOURCE_CONTEXT_CACHE[key] = (
        time.time() + SOURCE_CONTEXT_TTL, actual_name, rate_table
    )
    return actual_name, rate_table


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
                and (not drafts[key].get("exports") or drafts[key].get("pending_revision"))
                and recent(drafts[key].get("updated_at") if drafts[key].get("pending_revision") else drafts[key].get("created_at", batch["created_at"]))]
        if keys:
            result.append({**batch, "draft_ids": keys})
    return sorted(result, key=lambda b: b["created_at"], reverse=True)[:20]


def catalog():
    result = {}
    all_subjects = []
    subject_by_id = {}

    def root_subject(subject):
        seen = set()
        while subject.get("parent") and str(subject["parent"]) in subject_by_id:
            parent_id = str(subject["parent"])
            if parent_id in seen:
                break
            seen.add(str(subject["id"]))
            subject = subject_by_id[parent_id]
        return subject

    for kind, filename in CATALOG_FILES.items():
        records = legacy.read_json(ROOT / "mappings/platform" / (filename + ".json"))["records"]
        by_id = {str(r["id"]): r for r in records}
        if kind == "subjects":
            all_subjects = records
            subject_by_id = by_id
            for record in records:
                top = root_subject(record)
                record["group_name"] = str(top["code"]) + " · " + str(top["name"])
            records = [r for r in records if r.get("leaf")]
        else:
            for record in records:
                parent = by_id.get(str(record.get("parent", "")))
                path = str(record.get("path", "")).strip("|").split("|")
                if not parent and len(path) > 1:
                    parent = by_id.get(path[-2])
                if parent:
                    record["group_name"] = str(parent["code"]) + " · " + parent["name"]
                elif kind == "customers" and record.get("customerClass_name"):
                    record["group_name"] = record["customerClass_name"]
                elif kind == "customers":
                    inferred = inferred_customer_group(record.get("name"))
                    if inferred:
                        record["group_name"] = inferred
        if kind == "inventory":
            records = [r for r in records if r.get("isEnd")]
            for record in records:
                category = legacy.PRODUCT_CODE_MAPPING.get(str(record["code"])[:4], "其他产品")
                record["subject_code"] = legacy.INVENTORY_SUBJECT[category]
                record["subject_name"] = category
                if not record.get("group_name"):
                    record["group_name"] = record["subject_code"] + " · " + category
        result[kind] = records
        result[kind + "_by_code"] = {str(r["code"]): r for r in records}
    subject_by_code = {str(row["code"]): row for row in all_subjects}

    aux = [
        row for row in legacy.read_json(
            ROOT / "mappings/platform/科目辅助核算配置.json"
        )["records"]
        if subject_by_code.get(str(row["account_code"]), {}).get("leaf")
    ]
    result["aux"] = aux
    result["aux_by_code"] = {r["account_code"]: r["auxiliary_fields"] for r in aux}
    for row in aux:
        subject = subject_by_code[str(row["account_code"])]
        root = root_subject(subject)
        row["group_name"] = str(root["code"]) + " · " + str(root["name"])
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
        row["account_name"] = subject.get("displayname", row["account_name"])
    for kind in (*CATALOG_FILES, "aux"):
        for row in result[kind]:
            if row.get("group_override") is not None:
                row["group_name"] = row["group_override"]
    return result


def inferred_customer_group(name):
    value = str(name or "").strip()
    chinese = sum("\u3400" <= char <= "\u9fff" for char in value)
    latin = sum(("A" <= char <= "Z") or ("a" <= char <= "z") for char in value)
    if chinese and chinese >= latin:
        return "国内"
    if latin:
        return "国外"
    return ""


def catalog_key(kind, row):
    return str(row.get("record_id") or row.get("id")) if kind == "rates" else str(row.get("account_code") if kind == "aux" else row.get("code"))


def group_override(item, old=None):
    value = item.get("group_override", (old or {}).get("group_override"))
    if value is None:
        return None
    if not isinstance(value, str) or len(value.strip()) > 80 or any(ord(c) < 32 for c in value):
        fail(422, "分组名称须为80字以内的单行文本")
    return value.strip()


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
                            "币种": [currency], "汇率": str(value), "数据来源": str(fields.get("数据来源", "" if old else "本地维护"))}}
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
    if kind != "rates":
        patch["group_override"] = group_override(item, old)
    with LOCK, store.connect(True) as db:
        state = store.get(db, "settings", "catalog_edits") or {"version": 1, "items": {}}
        if payload.get("version") != state["version"]:
            fail(409, "数据源已被修改，请刷新后重试；当前输入已保留")
        if old:
            if kind == "rates":
                a, b = old["fields"], patch["fields"]
                unchanged = (engine.number(a.get("汇率")) == engine.number(b["汇率"])
                             and legacy.to_text(a.get("数据来源")) == legacy.to_text(b.get("数据来源")))
            elif kind == "subjects":
                unchanged = all(old.get(k) == patch.get(k) for k in ("name", "direction", "group_override"))
            else:
                signature = lambda fields: sorted((f["code"], bool(f.get("mustinput"))) for f in fields)
                unchanged = (signature(old["auxiliary_fields"]) == signature(patch["auxiliary_fields"])
                             and old.get("group_override") == patch.get("group_override"))
            if unchanged:
                return {"version": state["version"], "changed": False}
        state["items"].setdefault(kind, {})[key] = patch
        state["version"] += 1
        store.put(db, "settings", "catalog_edits", state)
        mappings = mapping_state(db)
        mappings["version"] += 1
        store.put(db, "settings", "mappings", mappings)
        store.audit(db, "catalog:" + kind + ":" + key, session, "save_catalog", old, patch)
    legacy.MASTERS = None
    return {"version": state["version"], "changed": True}


def mapping_state(db):
    stored = store.get(db, "settings", "mappings")
    state = copy.deepcopy(stored)
    if state:
        return merge_domestic_customer_mappings(state)
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
    return merge_domestic_customer_mappings(state)


def domestic_customer_mapping_rows():
    rules_path = ROOT / "data/domestic_voucher_rules_fit.json"
    customers_path = ROOT / "mappings/platform/客户档案.json"
    source_paths = [
        rules_path,
        customers_path,
        ROOT / "data/domestic_source_current.json",
        ROOT / "data/ai_customs_source.json",
        ROOT / "data/ruibei_buyer_mapping_2026.json",
    ]
    signature = (
        str(ROOT),
        tuple(
            (str(path), path.stat().st_mtime_ns, path.stat().st_size)
            if path.exists() else (str(path), 0, 0)
            for path in source_paths
        ),
    )
    return list(_domestic_customer_mapping_rows_cached(signature))


@lru_cache(maxsize=16)
def _domestic_customer_mapping_rows_cached(signature):
    rules_path = ROOT / "data/domestic_voucher_rules_fit.json"
    customers_path = ROOT / "mappings/platform/客户档案.json"
    if not rules_path.exists() or not customers_path.exists():
        return []
    rules = legacy.read_json(rules_path)
    known = (rules.get("mappings") or {}).get("customers") or {}
    platform = legacy.read_json(customers_path).get("records", [])
    by_code = {str(row["code"]): row for row in platform if row.get("code")}
    by_name = {}
    for row in platform:
        name = legacy.normalize_name(row.get("name"))
        if name:
            by_name.setdefault(name, str(row["code"]))

    names = []
    for filename in ("domestic_source_current.json", "ai_customs_source.json"):
        path = ROOT / "data" / filename
        if not path.exists():
            continue
        for item in legacy.read_json(path).get("records", []):
            fields = item.get("fields", item)
            if domestic.text(fields.get("销售方式")) != "内销":
                continue
            names.extend([
                domestic.text(fields.get("客户名称_脚本用")),
                domestic.text(fields.get("买方英文名")),
            ])
    ruibei_path = ROOT / "data/ruibei_buyer_mapping_2026.json"
    if ruibei_path.exists():
        names.extend(
            domestic.text(item.get("公司名称"))
            for item in legacy.read_json(ruibei_path).get("results", [])
            if item.get("状态") == "已获取"
        )

    rows, seen = [], set()
    for alias in names:
        alias = domestic.text(alias)
        normalized = legacy.normalize_name(alias)
        if not alias or not normalized or normalized in seen:
            continue
        # Chinese legal names should use an exact customer-archive match. English
        # names and abbreviations retain the fitted voucher mapping because the same
        # group may deliberately bill through a domestic subsidiary or trading entity.
        chinese_name = any("\u3400" <= char <= "\u9fff" for char in alias)
        code = str((by_name.get(normalized) if chinese_name else "") or known.get(normalized) or "")
        if not code:
            continue
        seen.add(normalized)
        archive = by_code.get(code)
        rows.append({
            "id": "domestic-customer-" + hashlib.sha256(
                (normalized + "\0" + code).encode("utf-8")
            ).hexdigest()[:20],
            "alias": alias,
            "platform_code": code,
            "platform_id": str(archive.get("id") or "") if archive else "",
            "platform_name": str(archive.get("name") or alias) if archive else alias,
            "enabled": True,
            "updated_at": "",
            "basis": "内销规则与睿贝买方映射自动同步",
        })
    return sorted(rows, key=lambda row: (row["alias"].casefold(), row["platform_code"]))


def merge_domestic_customer_mappings(state):
    state = copy.deepcopy(state)
    rows = state.setdefault("items", {}).setdefault("customers", [])
    aliases = {
        legacy.normalize_name(row.get("alias")): row
        for row in rows if row.get("alias")
    }
    for row in domestic_customer_mapping_rows():
        normalized = legacy.normalize_name(row["alias"])
        existing = aliases.get(normalized)
        if existing is None:
            rows.append(row)
            aliases[normalized] = row
        elif (existing.get("basis") == row["basis"]
              and existing.get("platform_code") != row["platform_code"]):
            existing.update(
                platform_code=row["platform_code"],
                platform_id=row["platform_id"],
                platform_name=row["platform_name"],
                updated_at=store.now(),
            )
    return state


def persist_mapping_migrations(db):
    stored = store.get(db, "settings", "mappings")
    state = mapping_state(db)
    if stored is None:
        store.put(db, "settings", "mappings", state)
    elif state != stored:
        state["version"] = int(stored.get("version", 1)) + 1
        store.put(db, "settings", "mappings", state)
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


def sync_mapping_file(kind, state):
    filename, key = MAP_FILES[kind]
    path = ROOT / "mappings" / filename
    payload = legacy.read_json(path) if path.exists() else {key: []}
    payload[key] = [r for r in state["items"][kind] if kind == "inventory" or r.get("enabled", True)]
    payload["updated_at"] = store.now()
    temp = path.with_suffix(".json.tmp")
    temp.write_text(store.dumps(payload), encoding="utf-8")
    temp.replace(path)


def sync_mapping_files(state):
    """Atomic compatibility mirrors; DB is authoritative and repairs mirrors at startup."""
    for kind in MAP_FILES:
        sync_mapping_file(kind, state)
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
                "enabled": bool(incoming.get("enabled", True)), "updated_at": store.now(),
                "group_override": group_override(incoming, old)}
        if old and all(old.get(k, True if k == "enabled" else None) == item.get(k)
                       for k in ("alias", "platform_code", "enabled", "group_override")):
            return state
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


def validate_task_payload(kind, payload):
    if contains_non_finite(payload):
        fail(422, "请求包含无效数值，请检查后重试")
    if kind == "query":
        if payload.get("mode", "pending") != "pending":
            fail(422, "按日期检索已停用，请使用飞书待开范围")
    elif kind == "build":
        if not isinstance(payload.get("snapshot_id"), str) or not payload["snapshot_id"]:
            fail(422, "请先检索并选择有效凭证范围")
        keys = payload.get("keys")
        if (not isinstance(keys, list) or not keys
                or any(not isinstance(key, str) or not key for key in keys)
                or len(set(keys)) != len(keys)):
            fail(422, "凭证范围为空或包含重复项")
        try:
            dt.date.fromisoformat(payload.get("posting_date", ""))
        except (TypeError, ValueError):
            fail(422, "制单日期格式应为 YYYY-MM-DD")
    elif kind == "export":
        drafts = payload.get("drafts")
        if not isinstance(drafts, list) or not drafts:
            fail(422, "请选择要导出的凭证")
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
               or not item["id"] or not isinstance(item.get("version"), int)
               or isinstance(item.get("version"), bool) or item["version"] < 1
               for item in drafts):
            fail(422, "导出凭证参数无效，请重新预览")
        if len({item["id"] for item in drafts}) != len(drafts):
            fail(422, "导出范围包含重复凭证")
    elif kind == "writeback":
        if not isinstance(payload.get("export_id"), str) or not payload["export_id"]:
            fail(422, "缺少回写导出记录")


def contains_non_finite(value):
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(contains_non_finite(key) or contains_non_finite(item)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(contains_non_finite(item) for item in value)
    return False


def create_task(kind, payload, session, key):
    if kind not in ("query", "build", "export", "writeback"):
        fail(422, "未知任务")
    if not key or len(key) > 150:
        fail(422, "缺少请求标识")
    validate_task_payload(kind, payload)
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
    with store.connect(True) as db:
        tasks = store.all_items(db, "task")
        state = persist_mapping_migrations(db)
    if state and not OFFLINE:
        sync_mapping_files(state)
    for task in reversed(tasks):
        if task["kind"] == "query" and task["payload"].get("mode", "pending") != "pending":
            if task["status"] in ("queued", "running"):
                task.update(status="failed", message="按日期检索已停用，请使用飞书待开范围",
                            updated_at=store.now())
                with store.connect(True) as db:
                    store.put(db, "task", task["id"], task)
            continue
        if task["status"] in ("queued", "running"):
            submit(task["id"])


def business_type(r):
    contract = legacy.to_text(r.get("合同号_1")).upper().removeprefix("PI-")
    method = legacy.to_text(r.get("销售方式"))
    if method == "内销" and contract:
        return "内销"
    if method == "出口" and contract.startswith("26MT"):
        return "外销"
    return ""


def in_scope(r):
    return bool(business_type(r))


def group_identity(r):
    kind = business_type(r)
    contract = legacy.to_text(r.get("合同号_1"))
    customs = legacy.to_text(r.get("报关单号"))
    if kind == "内销":
        return "内销:" + (contract or "missing:" + r["record_id"])
    if kind == "外销":
        return "外销:" + (customs or "missing:" + r["record_id"])
    # Keep out-of-scope siblings visible as a blocking condition on their
    # business group instead of silently dropping them.
    if legacy.to_text(r.get("销售方式")) == "内销":
        return "内销:" + (contract or "missing:" + r["record_id"])
    return "外销:" + (customs or "missing:" + r["record_id"])


def filtered_pending_records(task, url, token):
    """Search only pending groups; persist each page, including inheritance dependencies."""
    cp = task["checkpoint"]

    def search(key, conditions):
        state = cp.setdefault(key, {"records": [], "done": False})
        while not state["done"]:
            params = {"page_size": 500}
            if state.get("cursor"):
                params["page_token"] = state["cursor"]
            body = feishu._request(url + "/search?" + urlencode(params), method="POST", token=token,
                                   data={"filter": {"conjunction": "or", "conditions": conditions}})
            if body.get("code") != 0:
                raise feishu.FeishuError("飞书筛选失败，请检查字段类型与读取权限后重试")
            data = body.get("data") or {}
            more, cursor = data.get("has_more"), data.get("page_token")
            if more and (not cursor or cursor == state.get("cursor")):
                raise feishu.FeishuError("飞书筛选分页信息不完整")
            state["records"] += [dict(r.get("fields", {}), record_id=r["record_id"])
                                  for r in data.get("items", [])]
            state.update(cursor=cursor, done=not more)
            checkpoint(task, progress=sum(len(v.get("records", [])) for v in cp.values() if isinstance(v, dict)))
        return state["records"]

    def condition(field, value):
        return {"field_name": field, "operator": "is", "value": [value]}

    pending = search("pending_search", [condition("是否开凭证", "待开")])
    records = [r for r in pending if in_scope(r)]
    declarations = sorted({legacy.to_text(r.get("报关单号")) for r in records} - {""})
    for offset in range(0, len(declarations), 20):
        records += search("group_search_" + str(offset),
                          [condition("报关单号", v) for v in declarations[offset:offset + 20]])
    contracts = sorted({
        legacy.to_text(r.get("合同号_1"))
        for r in records
        if business_type(r) == "内销"
    } - {""})
    for offset in range(0, len(contracts), 20):
        records += search("domestic_group_search_" + str(offset),
                          [condition("合同号_1", v) for v in contracts[offset:offset + 20]])
    records = list({r["record_id"]: r for r in records}.values())
    parents = set()
    for record in records:
        contract = legacy.to_text(record.get("合同号_1"))
        match = legacy.ADD_SUFFIX.match(contract) if "&" not in contract else None
        if match:
            base = match.group("base")
            parents.add(base)
            if base.upper().endswith("Y"):
                parents.add(base[:-1])
    dependencies = []
    parents = sorted(parents)
    for offset in range(0, len(parents), 20):
        dependencies += search("parent_search_" + str(offset),
                               [condition("合同号_1", v) for v in parents[offset:offset + 20]])
    # Parent orders supply inheritance only; they must never become selected groups.
    cp["inheritance_records"] = dependencies
    return records


def query_job(task):
    payload, cp = task["payload"], task["checkpoint"]
    mode = payload.get("mode", "pending")
    if mode != "pending":
        fail(422, "按日期检索已停用，请使用飞书待开范围")
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
                actual_name, rate_table = verified_source_context(token, app_token, table_id)
                cp.update(verified=True, rate_table=rate_table, actual_name=actual_name)
                checkpoint(task)
            rate_path = ROOT / "data/fx_rates_ai_snapshot.json"
            rates_fresh = (
                rate_path.exists()
                and time.time() - rate_path.stat().st_mtime < RATE_CACHE_TTL
            )
            if not cp.get("rates_fetched") and not rates_fresh:
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
                temporary = rate_path.with_suffix(".json.tmp")
                temporary.write_text(
                    store.dumps({"records": cp["rates"], "fetched_at": store.now()}),
                    encoding="utf-8",
                )
                temporary.replace(rate_path)
                legacy.MASTERS = None
            url = f"{feishu.BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
            records = filtered_pending_records(task, url, token)
        cp.update(records=records, fetched=True)
        checkpoint(task, progress=len(records))
    # Deduplicate pages on service restart and preserve full declaration groups.
    records = list({r["record_id"]: r for r in records}.values())
    groups: dict[str, list[dict]] = {}
    selected = set()
    with store.connect() as db:
        occupied = {k for e in store.all_items(db, "export") for k in e["record_ids"]}
    excluded = 0
    for r in records:
        key = group_identity(r)
        groups.setdefault(key, []).append(r)
        if not in_scope(r):
            continue
        if legacy.to_text(r.get("是否开凭证")) == "已开" or r["record_id"] in occupied:
            excluded += 1
            continue
        if legacy.to_text(r.get("是否开凭证")) == "待开":
            selected.add(r["record_id"])
    result = []
    for key, group in groups.items():
        hits = [r for r in group if r["record_id"] in selected]
        if not hits:
            continue
        kind = business_type(hits[0])
        reasons = []
        if kind == "外销" and key.startswith("外销:missing:"):
            reasons.append("缺少报关单号，请在飞书补充后重新检索")
        if kind == "内销" and "missing:" in key:
            reasons.append("缺少合同号_1，请在飞书补充后重新检索")
        if any(r["record_id"] in occupied or legacy.to_text(r.get("是否开凭证")) == "已开" for r in group):
            label = "同一合同" if kind == "内销" else "同一报关单"
            reasons.append(label + "中已有已开或已导出记录")
        if any(not in_scope(r) for r in group):
            label = "同一合同" if kind == "内销" else "同一报关单"
            reasons.append(label + "包含范围外记录")
        statuses = {legacy.to_text(r.get("是否开凭证")) for r in group}
        if any(s not in ("", "待开", "未开", "未开凭证") for s in statuses):
            label = "同一合同" if kind == "内销" else "同一报关单"
            reasons.append(label + "存在需核实的凭证状态")
        contracts = list(dict.fromkeys(legacy.to_text(r.get("合同号_1")) for r in group))
        customs_no = legacy.to_text(group[0].get("报关单号")) if kind == "外销" else ""
        business_no = contracts[0] if kind == "内销" and contracts else customs_no
        result.append({"key": key, "business_type": kind, "business_no": business_no,
                       "customs_no": customs_no,
                       "contracts": contracts, "search_index": engine.contract_index(contracts),
                       "count": len(group), "matched": len(hits), "added": len(group) - len(hits),
                       "blocked": reasons,
                       "hard_blocked": [r for r in reasons if r in (
                           "缺少报关单号，请在飞书补充后重新检索",
                           "缺少合同号_1，请在飞书补充后重新检索",
                           "同一报关单包含范围外记录",
                           "同一合同包含范围外记录",
                       )],
                       "records": group})
    source_label = (cp.get("actual_name", "") + " / 2026年报关数据"
                    if cp.get("actual_name") else initial_source_label())
    snapshot = {"id": task["id"], "created_at": store.now(), "mode": mode, "groups": result,
                "matched": len(selected), "excluded": excluded,
                "source": source_label, "offline": OFFLINE,
                "all_records": list({r["record_id"]: r for r in cp.get("inheritance_records", []) + records}.values())}
    with store.connect(True) as db:
        store.put(db, "snapshot", snapshot["id"], snapshot)
        store.put(db, "settings", "source", {"label": source_label, "updated_at": store.now()})
    return {"snapshot_id": snapshot["id"], "groups": len(result), "matched": len(selected)}


def build_voucher_rows(records, masters, catalogs, kind):
    if kind != "内销":
        return engine.build(records, masters, catalogs)
    rules_path = ROOT / "data/domestic_voucher_rules_fit.json"
    if not rules_path.exists():
        raise ValueError("缺少内销凭证拟合规则，请先运行内销规则拟合")
    rules = legacy.read_json(rules_path)
    try:
        result = domestic.build_domestic_voucher(records, rules, masters)
    except domestic.DomesticVoucherError as error:
        raise ValueError("内销凭证构建失败：" + str(error)) from error
    rows = []
    for index, row in enumerate(result["rows"]):
        item = dict(row)
        item["id"] = item.get("id") or f"domestic:{index}"
        rows.append(item)
    issues = result.get("issues")
    if issues is None:
        issues = []
        for index, warning in enumerate(result["warnings"]):
            warning_index = int(warning.get("记录") or 0) - 1
            issues.append({
                "id": f"domestic-warning:{index}",
                "message": warning.get("原因", "采购信息不完整"),
                "row_id": "",
                "field": "",
                "record_id": result["record_ids"][warning_index]
                if 0 <= warning_index < len(result["record_ids"]) else "",
                "source_field": "",
                "severity": "warning",
            })
    return rows, issues


def build_job(task):
    p = task["payload"]
    try:
        dt.date.fromisoformat(p["posting_date"])
    except (KeyError, TypeError, ValueError):
        fail(422, "制单日期格式应为 YYYY-MM-DD")
    with store.connect() as db:
        snap = required(db, "snapshot", p["snapshot_id"])
        masters, version = masters_for(db)
    keys = set(p["keys"])
    groups = [g for g in snap["groups"] if g["key"] in keys]
    if not groups or len(groups) != len(keys):
        fail(422, "请选择有效凭证范围")
    acknowledged = set(p.get("acknowledged_groups", []))
    hard_messages = {
        "缺少报关单号，请在飞书补充后重新检索",
        "缺少合同号_1，请在飞书补充后重新检索",
        "同一报关单包含范围外记录",
        "同一合同包含范围外记录",
    }
    for g in groups:
        hard = g.get("hard_blocked", [r for r in g["blocked"] if r in hard_messages])
        if hard or (g["blocked"] and g["key"] not in acknowledged):
            fail(422, "请主动勾选已核实的异常分组；缺少业务单号或范围外记录仍需修复")
    catalogs = catalog()
    inherited, notes = legacy.apply_add_inheritance(copy.deepcopy(snap["all_records"]))
    by_id = {r["record_id"]: r for r in inherited}
    batch = {"id": task["id"], "created_at": task["created_at"], "draft_ids": [],
             "snapshot_id": snap["id"], "mode": snap["mode"]}
    for i, group in enumerate(groups):
        key = hashlib.sha256(group["key"].encode()).hexdigest()[:24]
        with store.connect(True) as db:
            draft = store.get(db, "draft", key)
            stale_empty_domestic = (
                draft is not None
                and group.get("business_type") == "内销"
                and not draft.get("rows")
                and bool(draft.get("build_issues"))
            )
            if draft is None or stale_empty_domestic:
                previous = draft
                source = copy.deepcopy(group["records"])
                effective = [by_id[r["record_id"]] for r in source]
                rows, issues = build_voucher_rows(
                    effective, masters, catalogs, group.get("business_type", "外销")
                )
                draft = {"id": key, "customs_no": group["customs_no"],
                         "business_no": group.get("business_no", group["customs_no"]),
                         "business_type": group.get("business_type", "外销"),
                         "contracts": group["contracts"],
                         "search_index": group["search_index"], "source": source, "overrides": {},
                         "effective_source": effective,
                         "header": {"posting_date": p["posting_date"], "category": "记", "maker": "王益"},
                         "rows": rows, "build_issues": issues, "blocking_issues": [],
                         "manual_fields": previous.get("manual_fields", []) if previous else [],
                         "version": previous["version"] + 1 if previous else 1,
                         "mapping_version": version,
                         "updated_at": store.now(),
                         "created_at": previous.get("created_at", store.now()) if previous else store.now(),
                         "exports": previous.get("exports", []) if previous else [],
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
            if group["key"] in acknowledged and draft.get("status_acknowledgement", {}).get("task_id") != task["id"]:
                draft["status_acknowledgement"] = {"reasons": group["blocked"], "session": task["session"], "at": store.now(), "task_id": task["id"]}
                if draft.get("exports") and draft.get("reopened_task") != task["id"]:
                    draft.update(pending_revision=True, reopened_task=task["id"], updated_at=store.now(), version=draft["version"] + 1)
                store.put(db, "draft", key, draft)
                store.audit(db, key, task["session"], "acknowledge_status", None, draft["status_acknowledgement"])
            batch["draft_ids"].append(key)
            store.put(db, "batch", batch["id"], batch)
        checkpoint(task, progress=i + 1, total=len(groups))
    return {"batch_id": batch["id"]}


def save_draft(key, payload, session):
    if contains_non_finite(payload):
        fail(422, "请求包含无效数值，请检查后重试")

    def normalize_row(row):
        normalized = {}
        for field, value in row.items():
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (str, int, float))
            ):
                fail(422, "分录字段值必须是文本或数字")
            normalized[str(field)] = "" if value is None else str(value)
        return normalized

    with store.connect(True) as db:
        old = required(db, "draft", key)
        if payload.get("version") != old["version"]:
            fail(409, "该凭证已被其他窗口修改，本地输入已保留，请比较后重新保存")
        draft = copy.deepcopy(old)
        if payload.get("manual_exception") is not None:
            draft["manual_exception"] = bool(payload["manual_exception"])
        draft["header"] = payload["header"]
        draft["rows"] = [normalize_row(row) for row in payload["rows"]]
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
            if any(not isinstance(value, str) for value in fields.values()):
                fail(422, "来源补充值必须是文本")
        prior = {r["id"]: normalize_row(r) for r in old["rows"]}
        manual = set(old["manual_fields"])
        if [r["id"] for r in old["rows"]] != [r["id"] for r in draft["rows"]]:
            manual.add("__rows__/structure")

        def same_cell(before, after):
            if before == after:
                return True
            before_number = engine.number(before)
            after_number = engine.number(after)
            return before_number is not None and after_number is not None and before_number == after_number

        for row in draft["rows"]:
            for field in row:
                if field != "id" and not same_cell(
                    row.get(field), prior.get(row["id"], {}).get(field)
                ):
                    manual.add(row["id"] + "/" + field)
        draft["manual_fields"] = sorted(manual)
        draft["version"] += 1
        draft["updated_at"] = store.now()
        engine.validate(draft, catalog())
        store.put(db, "draft", key, draft)
        store.audit(db, key, session, "edit", old, draft)
    return draft


def delete_batch(key, session):
    with store.connect(True) as db:
        batch = required(db, "batch", key)
        if any(task["status"] in ("queued", "running")
               for task in store.all_items(db, "task")):
            fail(409, "当前有任务正在处理，请等待任务结束后再删除生成记录")
        batches = [item for item in store.all_items(db, "batch") if item["id"] != key]
        shared = {draft_id for item in batches for draft_id in item.get("draft_ids", [])}
        deleted_drafts = []
        for draft_id in batch.get("draft_ids", []):
            if draft_id in shared:
                continue
            draft = store.get(db, "draft", draft_id)
            if draft is not None:
                store.audit(db, draft_id, session, "delete", draft, None)
                store.delete(db, "draft", draft_id)
                deleted_drafts.append(draft_id)
        store.audit(db, "batch:" + key, session, "delete", batch, None)
        store.delete(db, "batch", key)
    return {
        "deleted": key,
        "draft_count": len(batch.get("draft_ids", [])),
        "deleted_drafts": deleted_drafts,
    }


def recalculate(key, payload, session):
    with store.connect(True) as db:
        return _recalculate(db, key, payload, session)


def _recalculate(db, key, payload, session):
    old = required(db, "draft", key)
    if old["version"] != payload["version"]:
        fail(409, "凭证版本已更新，请刷新后重试")
    if old["manual_fields"] and not (payload.get("replace_manual") or payload.get("preserve_manual")):
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
    draft["rows"], draft["build_issues"] = build_voucher_rows(
        records, masters, catalogs, old.get("business_type", "外销")
    )
    if payload.get("preserve_manual"):
        if "__rows__/structure" in old["manual_fields"]:
            draft["rows"] = copy.deepcopy(old["rows"])
        else:
            prior = {r["id"]: r for r in old["rows"]}
            for row in draft["rows"]:
                for field in row:
                    if row["id"] + "/" + field in old["manual_fields"] and row["id"] in prior:
                        row[field] = prior[row["id"]].get(field, "")
    draft["calculated_overrides"] = copy.deepcopy(draft["overrides"])
    draft["contracts"] = list(dict.fromkeys(legacy.to_text(r.get("合同号_1")) for r in records))
    draft["search_index"] = engine.contract_index(draft["contracts"])
    draft.update(manual_fields=old["manual_fields"] if payload.get("preserve_manual") else [], mapping_stale=False, mapping_version=version, version=old["version"] + 1, updated_at=store.now())
    engine.validate(draft, catalogs)
    store.put(db, "draft", key, draft)
    store.audit(db, key, session, "recalculate", old, draft)
    return draft


def mapping_recalculation_plan():
    with store.connect() as db:
        version = mapping_state(db)["version"]
        drafts = [d for d in store.all_items(db, "draft")
                  if not d.get("exports") and d["mapping_version"] != version]
    return {"mapping_version": version, "drafts": [
        {"id": d["id"], "version": d["version"], "manual": bool(d["manual_fields"]),
         "incoming_source": bool(d.get("incoming_source"))} for d in drafts]}


def recalculate_mappings(payload, session):
    # One transaction: a conflict or calculation failure rolls back the entire update.
    with store.connect(True) as db:
        if mapping_state(db)["version"] != payload.get("mapping_version"):
            fail(409, "映射再次更新，请重新发起统一计算")
        targets = payload.get("drafts", [])
        if not targets or len({d["id"] for d in targets}) != len(targets):
            fail(422, "请选择需要更新的草稿")
        for item in targets:
            old = required(db, "draft", item["id"])
            if old.get("exports") or old["version"] != item["version"]:
                fail(409, "草稿已修改或导出，请重新发起统一计算")
        return [_recalculate(db, item["id"], {**item,
                 "replace_manual": payload.get("replace_manual", False),
                 "preserve_manual": payload.get("preserve_manual", False),
                 "refresh_source": payload.get("refresh_source", False)}, session) for item in targets]


def export_plan(ids):
    valid, excluded = [], []
    with store.connect() as db:
        catalogs = catalog()
        for key in dict.fromkeys(ids):
            draft = engine.validate(required(db, "draft", key), catalogs)
            entry = {"id": key, "contracts": draft["contracts"],
                     "business_type": draft.get("business_type", "外销"),
                     "business_no": draft.get("business_no", draft.get("customs_no", "")),
                     "customs_no": draft.get("customs_no", ""), "version": draft["version"],
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


def validate_export_capacity(drafts):
    if 1 + sum(len(draft["rows"]) for draft in drafts) > 65536:
        fail(422, "超出xls单表行数限制，请减少导出范围")


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
            validate_export_capacity(drafts)
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
                draft.pop("pending_revision", None)
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
