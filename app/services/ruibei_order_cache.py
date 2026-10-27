"""Order-scoped customers from both ERP order modules; never infer accounting type."""
from __future__ import annotations

import datetime as dt
import json
import hashlib
import re
from pathlib import Path

CACHE_PATH = Path(__file__).resolve().parents[2] / ".chche/domestic_orders.json"
SOURCE = "ruibei_domestic_orders"


def record_key(record):
    return (record.get("module", "domestic"), record["order_id"])


def complete_modules(cache):
    return all(cache.get("modules", {}).get(module, {}).get("complete")
               for module in ("domestic", "export"))


def normalize_contract(value):
    value = re.sub(r"\s+", "", str(value or "").upper()).removeprefix("PI-")
    return re.sub(r"[-_]*ADD(?=\d|$)", "ADD", value)


def contract_keys(value):
    """Expand explicit joint ADD suffixes; never use arbitrary substring matching."""
    raw = re.sub(r"\s+", "", str(value or "").upper()).removeprefix("PI-")
    keys = {normalize_contract(raw)} if raw else set()
    if "&" not in raw:
        return keys
    first, *rest = raw.split("&")
    expanded = [first]
    for part in rest:
        if re.match(r"\d{2}MT-", part):
            expanded.append(part)
        elif re.fullmatch(r"ADD\d+(?:-[A-Z])?", part):
            base = re.sub(r"[-_]*ADD\d*(?:-[A-Z])?$", "", first)
            expanded.append(base + part)
        elif re.fullmatch(r"\d+[A-Z]*", part):
            match = re.match(r"^(.*?[A-Z])\d+[A-Z]*$", first)
            if not match:
                return keys
            expanded.append(match.group(1) + part)
        else:
            return keys
    keys.update(normalize_contract(part) for part in expanded)
    return keys


def account_fingerprint(values):
    return hashlib.sha256((values.get("RUI_BEI_ERP_URL", "") + "|" + values.get("RUI_BEI_ERP_NAME", "")).encode()).hexdigest()


def load_cache(path=None):
    path = Path(path or CACHE_PATH)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    # Approval-form snapshots cannot silently re-enter the new customer path.
    if not isinstance(value, dict) or value.get("source") != SOURCE or not value.get("complete"):
        return {}
    rows = value.get("records")
    if not isinstance(rows, list) or any(not isinstance(r, dict) or not r.get("order_id") or not r.get("order_code") for r in rows):
        return {}
    if len({record_key(r) for r in rows}) != len(rows):
        return {}
    if value.get("account_fingerprint"):
        from .feishu_service import config
        if value["account_fingerprint"] != account_fingerprint(config()):
            return {}
    return value


def save_cache(value, path=None):
    path = Path(path or CACHE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def resolve(cache, contract, *, allow_add=True):
    key = normalize_contract(contract)
    matched = [r for r in cache.get("records", []) if key in contract_keys(r.get("order_code"))]
    inherited = ""
    if not matched and allow_add and "&" not in key:
        base = re.sub(r"ADD\d*(?:-[A-Z])?$", "", key)
        if base != key:
            matched = [r for r in cache.get("records", []) if base in contract_keys(r.get("order_code"))]
            if matched:
                inherited = base
    names = {re.sub(r"[\s()（）]", "", str(r.get("customer_name") or "")).casefold()
             for r in matched if str(r.get("customer_name") or "").strip()}
    if not matched:
        status = "未找到内销订单"
    elif len(names) > 1:
        status = "内销订单客户冲突"
    elif not names or any(not str(r.get("customer_name") or "").strip() for r in matched):
        status = "内销订单客户字段为空"
    else:
        status = "已获取"
    modules = {r.get("module", "domestic") for r in matched}
    source_type = "、".join(name for module, name in (("domestic", "内销订单"), ("export", "外销订单")) if module in modules) or "内销及外销订单"
    return {"合同号_1": contract, "状态": status,
            "公司名称": matched[0]["customer_name"] if status == "已获取" else "",
            "来源类型": source_type + "客户名称", "继承自": inherited,
            "订单": [{k: r.get(k, "") for k in ("order_id", "order_code", "customer_id", "customer_name", "company_name", "module")}
                    for r in matched]}


def needs_remote(cache, contract, ttl_seconds=300):
    if not cache:
        return True
    if resolve(cache, contract, allow_add=False)["订单"]:
        return False
    checked = cache.get("queries", {}).get(normalize_contract(contract), {}).get("checked_at")
    # A just-fetched full snapshot also proves a current miss, allowing ADD inheritance.
    checked = checked or cache.get("fetched_at")
    try:
        stamp = dt.datetime.fromisoformat(checked)
        age = (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds()
        return age < 0 or age > ttl_seconds
    except (TypeError, ValueError):
        return True


def parse_rows(payload, module="domestic"):
    if not isinstance(payload, dict) or "root" not in payload or "total" not in payload:
        raise ValueError("内销订单列表响应格式不符，请检查登录及页面权限")
    records = []
    for columns in payload["root"]:
        values = {}
        for column in columns:
            names = str(column.get("beanName") or "").split(",")
            cells = column.get("columnValues") or []
            for name, value in zip(names, cells):
                if name:
                    values[name] = str(value or "").strip()
        # The export list appends an explicit totals row on each page.
        if module == "export" and not values.get("orderId") and values.get("orderCode") == "合计":
            continue
        if not values.get("orderId") or not values.get("orderCode"):
            raise ValueError("内销订单缺少编号或ID，已停止保存以避免错位")
        records.append({"order_id": values["orderId"], "order_code": values["orderCode"],
                        "module": module,
                        "customer_id": values.get("comId", ""), "customer_name": values.get("comName" if module == "export" else "customerName", ""),
                        "company_name": values.get("companyName", ""), "status": values.get("statusName", ""),
                        "contract_date": values.get("contractDate", "")})
    return records, int(payload["total"])
