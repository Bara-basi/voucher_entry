"""Decimal-based draft builder. Missing values remain visible and editable."""
from __future__ import annotations

import copy
import datetime as dt
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from . import voucher_service as legacy
from .workbench_store import TZ

NUMBERS = ["汇率", "原币借方", "原币贷方", "借方金额", "贷方金额", "借方数量", "贷方数量", "单价"]
AUX = {"客户编码": "customers", "供应商编码": "suppliers", "存货档案": "inventory"}
SOURCE_FIELDS = list(dict.fromkeys(legacy.REQUIRED_FIELDS + [
    "买方英文名", "客户名称_脚本用", "供应商简称", "报关重量", "出口日期", "汇率",
    "进项税额", "退税额", "销项税额", "运费", "保费"]))


def number(value):
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and abs(result) < Decimal("1e14") else None
    except (InvalidOperation, ValueError):
        return None


def money(value):
    return value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP) if value is not None else None


def string(value):
    return str(value) if value is not None else ""


def contract_index(contracts):
    """Index original and expanded joint-order spellings without changing business keys."""
    values = []
    for raw in contracts:
        raw = str(raw).upper().removeprefix("PI-").replace(" ", "")
        values.append(raw)
        previous = ""
        for part in raw.split("&"):
            if re.match(r"\d{2}MT-", part):
                expanded = part
            elif re.match(r"\d{2}[A-Z]", part) and previous:
                expanded = previous[:5] + part
            elif re.match(r"\d{3}", part) and previous:
                expanded = previous[:8] + part
            elif part.startswith("ADD") and previous:
                expanded = re.sub(r"-?ADD\d*.*$", "", previous) + "-" + part
            else:
                expanded = part
            values.extend([expanded, re.sub(r"[-_\s]", "", expanded)])
            previous = expanded
    return " ".join(values)


def build(records, masters, catalogs):
    rows, issues = [], []
    first = records[0]
    contract = legacy.to_text(first.get("合同号_1"))
    currency = legacy.to_text(first.get("币种")).upper()
    export_ts = number(first.get("出口日期"))
    rate = None
    if export_ts is not None:
        moment = dt.datetime.fromtimestamp(float(export_ts) / 1000, TZ)
        rate = number(masters.rates.get((str(moment.year), str(moment.month), currency)))
    rate = rate or number(first.get("汇率"))
    if currency in ("CNY", "RMB"):
        currency, rate = "CNY", Decimal(1)

    def issue(message, rid="", field="", source_id="", source_field="", severity="error"):
        issues.append({"id": f"{rid}:{field}:{source_id}:{source_field}:{len(issues)}",
                       "message": message, "row_id": rid, "field": field,
                       "record_id": source_id, "source_field": source_field, "severity": severity})

    def value(record, field, required=False):
        raw = record.get(field)
        n = number(raw)
        if n is None and raw not in (None, ""):
            issue("数值格式无效，请补充来源后重新计算", source_id=record["record_id"], source_field=field)
        if n is None and required:
            return None
        return n if n is not None else Decimal(0)

    def total(field, required=False):
        vals = [value(r, field, required) for r in records]
        return None if None in vals else sum(vals, Decimal(0))

    def add(key, summary, subject, local, original=None, debit=True, curr="CNY", **aux):
        row = {"id": key, "摘要": summary, "科目编码": subject or "", "币种名称": curr,
               "汇率": string(rate if curr != "CNY" else Decimal(1)),
               "原币借方": string(money(original if original is not None else local)) if debit else "0",
               "原币贷方": "0" if debit else string(money(original if original is not None else local)),
               "借方金额": string(money(local)) if debit else "0",
               "贷方金额": "0" if debit else string(money(local)),
               "客户编码": "", "供应商编码": "", "存货档案": "",
               "借方数量": "", "贷方数量": "", "单价": "", **aux}
        rows.append(row)
        return row

    def div(a, b):
        return a / b if a is not None and b else None

    def mul(a, b):
        return a * b if a is not None and b is not None else None

    account = legacy.ACCOUNT_BY_METHOD["出口"].get(currency, ("", ""))
    customer = masters.customer_code([first.get("买方英文名"), first.get("客户名称_脚本用")]) or ""
    revenue, tax = money(total("不含税人民币收入", True)), money(total("销项税额"))
    freight, insurance = money(mul(-total("运费"), rate)), money(mul(-total("保费"), rate))
    receivable = None if None in (revenue, tax, freight, insurance) else revenue + tax - freight - insurance
    fob = total("报关金额", True)
    revenue_original = div(total("不含税人民币收入", True), rate)
    ar = add("income:ar", contract + "外销收入", account[0], receivable, fob, curr=currency, 客户编码=customer)
    # Do not substitute computed local amounts for missing source foreign currency.
    ar["原币借方"] = string(money(fob))
    add("income:revenue", contract + "外销收入", account[1], revenue, revenue_original, curr=currency)
    if tax:
        add("income:tax", contract + "外销收入", "22210102", tax, debit=False)
    # Revenue is a credit, including foreign-currency credit.
    rr = rows[1]
    rr["贷方金额"], rr["借方金额"] = rr["借方金额"], "0"
    rr["原币贷方"], rr["原币借方"] = string(money(revenue_original)), "0"
    if total("运费"):
        add("income:freight", contract + "外销收入", "56010802", freight, -total("运费"), curr=currency)
    if total("保费"):
        add("income:insurance", contract + "外销收入", "560107", insurance, -total("保费"), curr=currency)
    if fob is not None and rate and receivable is not None and abs(receivable - money(fob * rate)) > 1:
        issue("收入与报关金额×汇率相差超过1元，请核对来源或修改并确认本行金额",
              "income:ar", "借方金额")
    if not customer:
        issue("未匹配客户：选择客户编码，或补充来源后重新计算", "income:ar", "客户编码",
              first["record_id"], "买方英文名")
    for record in records:
        rid = record["record_id"]
        summary = legacy.to_text(record.get("合同号_1")) or contract
        name = legacy.to_text(record.get("报关品名"))
        inventory = masters.inventory_code(name) or ""
        subject = (legacy.INVENTORY_SUBJECT[legacy.PRODUCT_CODE_MAPPING.get(inventory[:4], "其他产品")]
                   if inventory else "")
        supplier_raw = legacy.to_text(record.get("供应商"))
        supplier = "" if re.search(r"[,，、;；]", supplier_raw) else (
            masters.supplier_code(record.get("供应商简称"), supplier_raw) or "")
        cost, purchase = money(value(record, "不含税成本", True)), money(value(record, "采购金额", True))
        itax, rebate = value(record, "进项税额"), value(record, "退税额")
        effective = money(rebate if itax == 0 and rebate else itax)
        payable = cost + effective if cost is not None else None
        weight = value(record, "报关重量") or None
        unit = money(div(cost, weight))
        add(rid + ":stock", summary + "采购商品", subject, cost, 存货档案=inventory,
            借方数量=string(weight), 单价=string(unit))
        add(rid + ":tax", summary + "采购商品-专票", "22210103" if itax == 0 and rebate else "22210101", effective)
        add(rid + ":ap", summary + "采购商品", "220201", payable, debit=False, 供应商编码=supplier)
        add(rid + ":cost", summary + "结转采购成本", "540101", cost)
        add(rid + ":out", summary + "结转采购成本", subject, cost, debit=False,
            存货档案=inventory, 贷方数量=string(weight), 单价=string(unit))
        if purchase is None or (payable is not None and abs(payable - purchase) > 1):
            issue("采购金额缺失或与成本及税额不一致，请补充来源或核实应付金额",
                  rid + ":ap", "贷方金额", rid, "采购金额")
        if cost is not None and cost <= 0 and (purchase is None or purchase <= 0):
            issue("缺少有效采购成本，请补充来源或修正成本", rid + ":stock", "借方金额", rid, "不含税成本")
        if not inventory:
            issue("未匹配存货，请维护产品映射或选择有效存货和科目", rid + ":stock", "存货档案", rid, "报关品名")
        if not supplier:
            issue("供应商缺失、包含多个名称或无法匹配，请选择有效供应商",
                  rid + ":ap", "供应商编码", rid, "供应商")
    currencies = {legacy.to_text(r.get("币种")).upper() for r in records}
    if len(currencies) > 1:
        issue("同一报关单包含不同币种，请核实来源币种后重新计算")
    for record in records:
        for field in ("合同号_1", "销售方式"):
            if not legacy.to_text(record.get(field)):
                issue("缺少" + field, source_id=record["record_id"], source_field=field)
    return rows, issues


def validate(draft, catalogs):
    issues = copy.deepcopy(draft.get("blocking_issues", []))
    manual = set(draft.get("manual_fields", []))
    for issue in draft.get("build_issues", []):
        target = issue["row_id"] + "/" + issue["field"]
        if not issue["row_id"] or target not in manual:
            issues.append(issue)

    def err(message, row="", field=""):
        issues.append({"id": f"validate:{row}:{field}:{len(issues)}", "message": message,
                       "row_id": row, "field": field, "severity": "error"})

    if draft.get("overrides", {}) != draft.get("calculated_overrides", {}):
        err("来源补充值已修改，请重新计算后导出")
    if draft.get("incoming_source"):
        err("飞书来源已有更新，请采用最新来源重新计算后导出")
    try:
        dt.date.fromisoformat(draft["header"]["posting_date"])
    except (ValueError, KeyError):
        err("制单日期无效", field="posting_date")
    if not draft["header"].get("maker", "").strip():
        err("请填写制单人", field="maker")
    if draft["header"].get("category") != "记":
        err("当前模板凭证类别须为记", field="category")
    debit, credit = Decimal(0), Decimal(0)
    if not draft["rows"]:
        err("至少需要一条分录")
    ids = [r.get("id") for r in draft["rows"]]
    if len(ids) != len(set(ids)) or not all(ids):
        err("分录标识重复或为空")
    for row in draft["rows"]:
        rid = row["id"]
        subject = catalogs["subjects_by_code"].get(row.get("科目编码"))
        if not subject or not subject.get("leaf"):
            err("请选择有效的末级会计科目", rid, "科目编码")
        if not str(row.get("摘要", "")).strip():
            err("请填写摘要", rid, "摘要")
        currency = row.get("币种名称")
        if currency not in legacy.ACCOUNT_BY_METHOD["出口"]:
            err("币种无效或尚未支持", rid, "币种名称")
        nums = {f: number(row.get(f)) for f in NUMBERS}
        for field in NUMBERS:
            if nums[field] is None and (field in NUMBERS[:5] or row.get(field) not in ("", None)):
                err("请输入有效数值", rid, field)
        if nums["汇率"] is not None and nums["汇率"] <= 0:
            err("汇率必须大于零", rid, "汇率")
        if currency == "CNY" and nums["汇率"] != 1:
            err("人民币汇率必须为1", rid, "汇率")
        for field in ("借方金额", "贷方金额", "原币借方", "原币贷方"):
            n = nums[field]
            if n is not None and n != money(n):
                err("金额最多保留两位小数", rid, field)
        d, c = nums["借方金额"], nums["贷方金额"]
        od, oc = nums["原币借方"], nums["原币贷方"]
        if d and c:
            err("同一分录不能同时有借方和贷方金额", rid, "贷方金额")
        if (d and oc) or (c and od):
            err("原币方向与本币方向不一致", rid, "原币借方")
        if currency == "CNY" and (d != od or c != oc):
            err("人民币原币金额应与本币一致", rid, "原币借方")
        debit += d or 0
        credit += c or 0
        for field, kind in AUX.items():
            code = row.get(field, "")
            if code and code not in catalogs[kind + "_by_code"]:
                err("编码不在有效档案中", rid, field)
        config = catalogs["aux_by_code"].get(row.get("科目编码"), [])
        names = {"客户": "客户编码", "供应商": "供应商编码", "存货档案": "存货档案"}
        for spec in config:
            field = names.get(spec.get("name"))
            # Customer/vendor/inventory dimensions used by this workbook are required.
            if field and not row.get(field):
                err("该科目需要" + spec["name"], rid, field)
            elif spec.get("mustinput") and not field:
                err("该科目要求尚未支持的辅助项：" + spec.get("name", ""), rid, "科目编码")
    difference = money(debit - credit)
    if not any(number(r.get("借方金额")) or number(r.get("贷方金额")) for r in draft["rows"]):
        err("凭证不能全部为零金额")
    if difference:
        err("借贷不平衡，差额 " + str(difference), field="balance")
    draft["issues"] = issues
    draft["totals"] = {"debit": str(money(debit)), "credit": str(money(credit)), "difference": str(difference)}
    draft["valid"] = not any(i["severity"] == "error" for i in issues)
    return draft
