"""Cache customer names from ERP domestic and export order lists."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".workdeps").is_dir():
    sys.path.insert(0, str(ROOT / ".workdeps"))
from playwright.sync_api import sync_playwright
from scripts.read_feishu import config
from app.services import ruibei_order_cache as orders


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def login(page, values):
    for key in ("RUI_BEI_ERP_URL", "RUI_BEI_ERP_NAME", "RUIB_BEI_ERP_PASSWORD"):
        if not values.get(key):
            raise KeyError(key)
    page.goto(values["RUI_BEI_ERP_URL"], wait_until="domcontentloaded", timeout=60000)
    page.locator("#userId").fill(values["RUI_BEI_ERP_NAME"])
    page.locator("#password").fill(values["RUIB_BEI_ERP_PASSWORD"])
    page.get_by_role("button", name="立即登录").click()
    page.locator('a[href="demosticSaleOrder?menuCode=13100"]').wait_for(state="attached", timeout=60000)


def open_order_list(page, module="domestic"):
    route, table = (("saleOrder?menuCode=80300", "saleOrder") if module == "export"
                    else ("demosticSaleOrder?menuCode=13100", "demosticSaleOrder"))
    # Wait for the initial list request before switching tabs. Otherwise its late
    # initialization can reset "全部" back to the default "未完成" condition.
    with page.expect_response(lambda response: urlsplit(response.url).path == f"/{table}_list", timeout=60000):
        page.locator(f'a[href="{route}"]').evaluate("e=>e.click()")
    page.wait_for_selector('iframe[name="mainFrame"]', state="attached")
    frame = next(f for f in page.frames if f.name == "mainFrame")
    frame.wait_for_load_state("load", timeout=60000)
    frame.locator(f"#{table}OutId #next_page").wait_for(state="attached", timeout=60000)
    def is_all_list(response):
        return (urlsplit(response.url).path == f"/{table}_list"
                and dict(parse_qsl(response.request.post_data or "")).get("condition", "") in ("", "0"))
    with page.expect_response(is_all_list, timeout=60000) as event:
        frame.locator('div.tab_btnBubbles[rel="tagContent0"]').click()
    response = event.value
    payload = dict(parse_qsl(response.request.post_data or ""))
    for key in ("contractDate_start", "contractDate_end", "condition", "searchValue", "p"):
        payload.pop(key, None)
    return response.url, payload


def fetch_page(context, endpoint, payload, page_number, search=""):
    form = dict(payload, p=str(page_number))
    if search:
        form["searchValue"] = search
    response = context.request.post(endpoint, form=form, timeout=60000)
    if not response.ok:
        raise RuntimeError("内销订单查询失败 HTTP " + str(response.status))
    try:
        module = "export" if urlsplit(endpoint).path == "/saleOrder_list" else "domestic"
        return orders.parse_rows(response.json(), module)
    except ValueError:
        raise RuntimeError("内销订单查询响应无效，请检查登录、页面权限及字段结构") from None


def download(context, endpoint, payload, checkpoint_path=None, fingerprint="", search=""):
    state = {}
    if checkpoint_path and checkpoint_path.exists():
        state = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if state.get("fingerprint") != fingerprint or state.get("complete"):
            state = {}
    records = state.get("records", [])
    total = state.get("total")
    page_number = state.get("next_page", 1)
    seen = {r["order_id"] for r in records}
    while total is None or len(records) < total:
        batch, expected = fetch_page(context, endpoint, payload, page_number, search)
        if total is not None and expected != total:
            if checkpoint_path:
                checkpoint_path.unlink(missing_ok=True)
            raise RuntimeError("抓取期间订单总数变化，请重试从第一页获取")
        total = expected
        if not batch and len(records) != total:
            raise RuntimeError("内销订单分页提前结束，已保留检查点")
        if any(r["order_id"] in seen for r in batch) or len({r["order_id"] for r in batch}) != len(batch):
            if checkpoint_path:
                checkpoint_path.unlink(missing_ok=True)
            raise RuntimeError("订单分页存在重复ID，请重试完整获取")
        records.extend(batch)
        seen.update(r["order_id"] for r in batch)
        if len(records) > total:
            raise RuntimeError("订单分页数量超过报告总数")
        if checkpoint_path:
            orders.save_cache({"fingerprint": fingerprint, "total": total, "records": records,
                "next_page": page_number + 1, "complete": len(records) == total}, checkpoint_path)
        print(f"订单页 {page_number}：{len(records)}/{total}", flush=True)
        page_number += 1
    return records, total


def search_variants(contract):
    raw = str(contract).strip().upper().removeprefix("PI-")
    values = [raw, orders.normalize_contract(raw)]
    match = re.match(r"^(.*?)(?:[-_\s]*)ADD(\d*)(-[A-Z])?$", raw)
    if match:
        base, index, suffix = match.group(1), match.group(2), match.group(3) or ""
        values.extend(base + separator + "ADD" + index + suffix for separator in ("-", "", "_", " "))
    return list(dict.fromkeys(values))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=orders.CACHE_PATH)
    parser.add_argument("--checkpoint", type=Path, default=orders.CACHE_PATH.with_name("domestic_orders.checkpoint.json"))
    parser.add_argument("--full", action="store_true", help="Fetch all visible orders, removing default date restrictions")
    parser.add_argument("--contract", action="append", default=[])
    parser.add_argument("--domestic", type=Path, help="Workbench request containing contracts to check")
    args = parser.parse_args()
    contracts = list(args.contract)
    if args.domestic:
        source = json.loads(args.domestic.read_text(encoding="utf-8"))
        contracts.extend(str(r.get("fields", r).get("合同号_1") or "") for r in source.get("records", []))
    contracts = sorted(set(c for c in contracts if c))
    values = config()
    fingerprint = orders.account_fingerprint(values)
    cache = orders.load_cache(args.output)
    full = args.full or not cache or cache.get("account_fingerprint") != fingerprint or not contracts
    with sync_playwright() as playwright:
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        browser = playwright.chromium.launch(headless=True, **({"executable_path": str(chrome)} if chrome.exists() else {}))
        try:
            context = browser.new_context(locale="zh-CN")
            page = context.new_page()
            login(page, values)
            if full or not orders.complete_modules(cache):
                snapshots = {}
                all_records = []
                for module in ("domestic", "export"):
                    endpoint, payload = open_order_list(page, module)
                    checkpoint_path = (args.checkpoint if module == "domestic" else
                                       args.checkpoint.with_name("export_orders.checkpoint.json"))
                    print("正在读取" + ("内销订单" if module == "domestic" else "外销订单"), flush=True)
                    records, total = download(context, endpoint, payload, checkpoint_path, fingerprint + "|" + module)
                    for record in records:
                        record["module"] = module
                    all_records.extend(records)
                    snapshots[module] = {"complete": True, "reported_total": total, "fetched_at": now()}
                cache = {"schema_version": 3, "source": orders.SOURCE, "complete": True,
                    "account_fingerprint": fingerprint, "fetched_at": now(), "reported_total": len(all_records),
                    "scope": "内销及外销订单：全部状态、无签约日期限制、当前账号可见", "records": all_records,
                    "modules": snapshots, "queries": {}}
                orders.save_cache(cache, args.output)
                full = True
            else:
                endpoints = {module: open_order_list(page, module) for module in ("domestic", "export")}
                for index, contract in enumerate(contracts, 1):
                    print(f"单合同查询 {index}/{len(contracts)}：{contract}", flush=True)
                    received = {}
                    # Prefixes guide query order only. Always check both modules for conflicts.
                    preferred = ("domestic", "export") if re.match(r"^(?:PI-)?\d{2}MT-DP", contract.upper()) else ("export", "domestic")
                    for module in preferred:
                        endpoint, payload = endpoints[module]
                        module_records = {}
                        for search in search_variants(contract):
                            batch, _ = download(context, endpoint, payload, search=search)
                            module_records.update({orders.record_key(r): r for r in batch})
                            if orders.resolve({"records": list(module_records.values())}, contract, allow_add=False)["订单"]:
                                break
                        received.update(module_records)
                    current = {orders.record_key(r): r for r in cache["records"]}
                    key = orders.normalize_contract(contract)
                    current = {k:r for k,r in current.items() if key not in orders.contract_keys(r["order_code"])}
                    current.update(received)
                    cache["records"] = list(current.values())
                    cache.setdefault("queries", {})[key] = {"checked_at": now(),
                        "status": orders.resolve(cache, contract, allow_add=False)["状态"]}
                    orders.save_cache(cache, args.output)
        finally:
            browser.close()
    print(json.dumps({"cached_orders": len(cache["records"]), "full_snapshot": full,
                      "output": str(args.output.resolve())}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
