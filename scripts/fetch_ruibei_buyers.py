"""Find missing domestic customer names through Ruibei ERP invoice applications.

The workflow follows the business process: login, OA -> approval center ->
general approval -> search contract -> open the invoice application -> read
``公司名称（必填）`` from the embedded form. Credentials are read from .env and
are never printed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from read_feishu import config

DEFAULT_DOMESTIC = ROOT / "data/domestic_source_current.json"
DEFAULT_OUTPUT = ROOT / "data/ruibei_buyer_mapping_2026.json"

BUYER_EN_FIELD = "买方英文名"
BUYER_CN_FIELD = "客户名称_脚本用"
CONTRACT_FIELD = "合同号_1"
METHOD_FIELD = "销售方式"
ADD_SUFFIX = re.compile(
    r"^(?P<base>.+?)(?:[-_\s]*)ADD(?P<index>\d*)(?:-(?P<letter>[A-Z]))?$",
    re.IGNORECASE,
)


def text(value) -> str:
    if isinstance(value, list):
        return "；".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in value
        ).strip()
    return str(value or "").strip()


def normalize_contract(value: str) -> str:
    value = str(value or "").upper().strip()
    if value.startswith("PI-"):
        value = value[3:]
    value = re.sub(r"\s+", "", value)
    return re.sub(r"[-_]*ADD(\d*)$", r"ADD\1", value)


def search_variants(contract: str) -> list[str]:
    values = [contract]
    match = ADD_SUFFIX.match(str(contract or "").strip())
    if match:
        base = match.group("base")
        index = match.group("index")
        letter = match.group("letter")
        suffix = ("-" + letter.upper()) if letter else ""
        values.extend(
            base + separator + "ADD" + index + suffix
            for separator in ("", "-", "_", " ")
        )
    else:
        values.append(re.sub(r"\s+", "", contract))
    return list(dict.fromkeys(values))


def add_base_contract(contract: str) -> str:
    match = ADD_SUFFIX.match(str(contract or "").strip())
    return match.group("base").strip() if match else ""


def load_missing_contracts(path: Path) -> tuple[dict, list[dict], list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    records = []
    for wrapped in data.get("records") or []:
        record = wrapped.get("fields") if isinstance(wrapped.get("fields"), dict) else wrapped
        if text(record.get(METHOD_FIELD)) != "内销":
            continue
        if text(record.get(BUYER_EN_FIELD)) or text(record.get(BUYER_CN_FIELD)):
            continue
        records.append(record)
    contracts = sorted({
        text(record.get(CONTRACT_FIELD))
        for record in records
        if text(record.get(CONTRACT_FIELD))
    })
    return data, records, contracts


def login(page: Page, values: dict) -> None:
    page.goto(values["RUI_BEI_ERP_URL"], wait_until="domcontentloaded", timeout=60000)
    page.locator("#userId").fill(values["RUI_BEI_ERP_NAME"])
    page.locator("#password").fill(values["RUIB_BEI_ERP_PASSWORD"])
    page.get_by_role("button", name="立即登录").click()
    page.locator('a[href="auditReason?menuCode=D101600"]').wait_for(
        state="attached", timeout=60000
    )


def enter_general_approval(page: Page):
    page.locator('a[href="auditReason?menuCode=D101600"]').evaluate("element => element.click()")
    page.wait_for_timeout(1500)
    frame = next((item for item in page.frames if item.name == "mainFrame"), None)
    if frame is None:
        raise RuntimeError("Approval center frame did not load")
    frame.get_by_text("通用审批", exact=True).first.click()
    frame.locator("#universalAudit_searchValue").wait_for(timeout=30000)
    return frame


def current_main_frame(page: Page):
    return next((item for item in page.frames if item.name == "mainFrame"), None)


def ensure_search_frame(page: Page, frame):
    try:
        if frame and frame.locator("#universalAudit_searchValue").count():
            return frame
    except Exception:
        pass
    page.wait_for_timeout(300)
    refreshed = current_main_frame(page)
    try:
        if refreshed and refreshed.locator("#universalAudit_searchValue").count():
            return refreshed
    except Exception:
        pass
    return enter_general_approval(page)


def search_contract(page: Page, frame, contract: str):
    frame = ensure_search_frame(page, frame)
    search = frame.locator("#universalAudit_searchValue")
    search.fill(contract)
    search.press("Enter")
    page.wait_for_timeout(3000)
    refreshed = current_main_frame(page)
    return refreshed or frame


def invoice_candidates(frame, contract: str, debug: bool = False) -> list[dict]:
    expected = normalize_contract(contract)
    candidates = []
    rows = frame.locator("#universalAuditList > li")
    for index in range(rows.count()):
        row = rows.nth(index)
        row_text = row.inner_text()
        if debug and "开票申请" in row_text:
            print({
                "row_index": index,
                "row_text": row_text[:500],
                "bills": row.locator("a[href*='commonViewDetailBillsByCode']").evaluate_all(
                    "(els) => els.map(e => ({text:e.innerText, href:e.getAttribute('href')}))"
                ),
            }, flush=True)
        if "开票申请" not in row_text:
            continue
        bill_links = row.locator("a[href*='commonViewDetailBillsByCode']")
        if not bill_links.count():
            continue
        related_bill = bill_links.first.inner_text().strip()
        if normalize_contract(related_bill) != expected:
            continue
        invoice_links = row.get_by_text("开票申请", exact=True)
        if not invoice_links.count():
            continue
        href = invoice_links.first.get_attribute("href") or ""
        match = re.search(r"commonViewDetailOrderById\(([^,]+),([^)]+)\)", href)
        candidates.append({
            "row_index": index,
            "related_bill": related_bill,
            "href": href,
            "audit_id": match.group(1) if match else "",
            "list_id": match.group(2) if match else "",
        })
    if debug and not candidates:
        invoice_links = frame.get_by_text("开票申请", exact=True)
        for index in range(min(invoice_links.count(), 5)):
            item = invoice_links.nth(index)
            print({
                "link_index": index,
                "href": item.get_attribute("href"),
                "ancestors": item.evaluate(
                    "(e) => { const out=[]; let n=e; for(let i=0;i<10 && n;i++,n=n.parentElement) "
                    "out.push({tag:n.tagName, cls:String(n.className||''), id:n.id, text:(n.innerText||'').slice(0,180)}); return out; }"
                ),
                "html": item.evaluate(
                    "(e) => { let n=e; for(let i=0;i<5 && n;i++,n=n.parentElement) { "
                    "if(n.tagName==='TR' || n.getAttribute?.('role')==='row' || "
                    "String(n.className).toLowerCase().includes('row')) return n.outerHTML; } "
                    "return e.parentElement?.outerHTML; }"
                )[:3000],
            }, flush=True)
    return candidates


def company_from_detail(detail_page: Page) -> tuple[str, str]:
    detail_page.wait_for_timeout(1200)
    company = ""
    tax_id = ""
    for frame in detail_page.frames:
        try:
            body = frame.locator("body").inner_text(timeout=5000).replace("\xa0", " ")
        except Exception:
            continue
        if "公司名称" not in body:
            continue

        company_match = re.search(
            r"公司名称\s*[（(]必填[）)]\s*([^\r\n]+)", body
        )
        if company_match:
            company = company_match.group(1).strip()
        tax_match = re.search(
            r"纳税识别号[^\r\n]*必填\s*([^\r\n]+)", body
        )
        if tax_match:
            tax_id = tax_match.group(1).strip()
        if company:
            break
    return company, tax_id


def company_from_shipping_rows(frame, contract: str) -> list[dict]:
    expected = normalize_contract(contract)
    found = []
    rows = frame.locator("#universalAuditList > li")
    for index in range(rows.count()):
        row = rows.nth(index)
        row_text = row.inner_text()
        if "发货申请" not in row_text:
            continue
        bill_links = row.locator("a[href*='commonViewDetailBillsByCode']")
        if not bill_links.count():
            continue
        related_bill = bill_links.first.inner_text().strip()
        if normalize_contract(related_bill) != expected:
            continue
        warehouse = re.search(
            r"收货工厂/仓库[:：]\s*(.*?)(?=\s*发货时间|\s*预计到达|\s*发货过去的目的|$)",
            row_text.replace("\xa0", " "),
        )
        if not warehouse:
            continue
        for token in re.split(r"\s+", warehouse.group(1).strip()):
            token = token.strip()
            if re.search(r"(?:股份有限公司|有限公司|造船厂|工厂|公司)$", token):
                found.append({
                    "row_index": index,
                    "related_bill": related_bill,
                    "公司名称": token,
                    "来源字段": "发货申请通用审批 / 收货工厂/仓库",
                })
                break
    return found


def acquire_contract(context, page: Page, frame, contract: str, debug: bool = False):
    candidates = []
    shipping_companies = []
    searched_values = []
    for searched_value in search_variants(contract):
        searched_values.append(searched_value)
        frame = search_contract(page, frame, searched_value)
        if debug:
            print(frame.locator("body").inner_text()[:1500], flush=True)
        candidates = invoice_candidates(frame, contract, debug)
        if not candidates:
            shipping_companies.extend(company_from_shipping_rows(frame, contract))
            page.wait_for_timeout(1000)
            frame = ensure_search_frame(page, frame)
            candidates = invoice_candidates(frame, contract, debug)
            if not candidates:
                shipping_companies.extend(company_from_shipping_rows(frame, contract))
        if candidates:
            break

    result = {
        "合同号_1": contract,
        "状态": "未找到开票申请",
        "公司名称": "",
        "纳税识别号": "",
        "搜索值": searched_values,
        "开票申请数": len(candidates),
        "开票申请": [],
    }
    companies = []
    for candidate in candidates:
        row = frame.locator("#universalAuditList > li").nth(candidate["row_index"])
        link = row.get_by_text("开票申请", exact=True).first
        try:
            with context.expect_page(timeout=15000) as page_info:
                link.click()
            detail_page = page_info.value
        except Exception:
            detail_page = page
        try:
            company, tax_id = company_from_detail(detail_page)
            detail_url = detail_page.url
        finally:
            if detail_page is not page:
                detail_page.close()
        item = {
            **candidate,
            "url": detail_url,
            "公司名称": company,
            "纳税识别号": tax_id,
        }
        result["开票申请"].append(item)
        if company:
            companies.append(company)
            result["公司名称"] = company
            result["纳税识别号"] = tax_id

    unique_companies = sorted(set(companies))
    if unique_companies:
        result["状态"] = "已获取" if len(unique_companies) == 1 else "多公司需复核"
        result["公司名称"] = unique_companies[0]
    elif shipping_companies:
        unique_shipping = {}
        for item in shipping_companies:
            unique_shipping.setdefault(item["公司名称"], item)
        if len(unique_shipping) == 1:
            source = next(iter(unique_shipping.values()))
            result["状态"] = "已获取"
            result["来源类型"] = "发货审批收货单位"
            result["公司名称"] = source["公司名称"]
            result["来源单据"] = source
        else:
            result["状态"] = "多公司需复核"
            result["发货审批候选"] = list(unique_shipping.values())
    return frame, result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domestic", type=Path, default=DEFAULT_DOMESTIC)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--contract", action="append", help="Only process the specified contract")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--merge-existing", action="store_true")
    args = parser.parse_args()

    data, records, contracts = load_missing_contracts(args.domestic)
    if args.contract:
        contracts = [value for value in contracts if value in set(args.contract)]
    values = config()
    base_by_contract = {
        contract: base
        for contract in contracts
        if (base := add_base_contract(contract))
    }
    query_contracts = sorted(set(contracts) | set(base_by_contract.values()))
    query_results = {}

    with sync_playwright() as playwright:
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        options = {"headless": not args.headed}
        if chrome.exists():
            options["executable_path"] = str(chrome)
        browser = playwright.chromium.launch(**options)
        try:
            context = browser.new_context(
                locale="zh-CN", viewport={"width": 1600, "height": 1000}
            )
            page = context.new_page()
            login(page, values)
            frame = enter_general_approval(page)

            for index, contract in enumerate(query_contracts, start=1):
                print(f"[{index}/{len(query_contracts)}] {contract}", flush=True)
                frame, query_results[contract] = acquire_contract(
                    context, page, frame, contract, args.debug
                )
        finally:
            browser.close()

    results = []
    inherited_contracts = []
    for contract in contracts:
        direct = dict(query_results[contract])
        base = base_by_contract.get(contract)
        if direct["状态"] != "已获取" and base and query_results[base]["状态"] == "已获取":
            base_result = query_results[base]
            direct.update({
                "状态": "已获取",
                "来源类型": "ADD继承",
                "继承自": base,
                "公司名称": base_result["公司名称"],
                "纳税识别号": base_result["纳税识别号"],
                "继承的开票申请数": base_result["开票申请数"],
                "继承的开票申请": base_result["开票申请"],
                "继承的原单来源类型": base_result.get("来源类型") or "开票申请",
            })
            inherited_contracts.append(contract)
        results.append(direct)

    existing = (
        json.loads(args.output.read_text(encoding="utf-8"))
        if args.merge_existing and args.output.exists()
        else None
    )
    if existing:
        by_contract = {
            result["合同号_1"]: result for result in existing.get("results") or []
        }
        by_contract.update({result["合同号_1"]: result for result in results})
        results = list(by_contract.values())
        inherited_contracts = sorted(set(
            (existing.get("inherited_add_contracts") or []) + inherited_contracts
        ))

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "睿贝ERP / OA / 审批中心 / 通用审批 / 开票申请",
        "condition": {
            "销售方式": "内销",
            "买方英文名": "",
            "客户名称_脚本用": "",
        },
        "record_count": existing.get("record_count", len(records)) if existing else len(records),
        "contract_count": existing.get("contract_count", len(contracts)) if existing else len(contracts),
        "query_count": len(query_contracts),
        "base_queries": sorted(set(base_by_contract.values())),
        "inherited_add_contracts": inherited_contracts,
        "found_contract_count": sum(result["状态"] == "已获取" for result in results),
        "direct_found_contract_count": sum(
            result["状态"] == "已获取" and result.get("来源类型") != "ADD继承"
            for result in results
        ),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "records": len(records),
        "contracts": len(contracts),
        "found": payload["found_contract_count"],
        "not_found": sum(result["状态"] == "未找到开票申请" for result in results),
        "needs_review": sum(result["状态"] == "多公司需复核" for result in results),
        "output": str(args.output.resolve()),
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
