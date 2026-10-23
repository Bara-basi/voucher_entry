"""Fit domestic-voucher rules and build vouchers without reading target vouchers.

The fitting phase uses historical Yonyou voucher samples to learn accounts,
summaries, auxiliary-account aliases and line ordering.  The generation phase
receives only the Feishu domestic records plus the serialized fitted rules.
"""

from __future__ import annotations

import collections
import copy
import datetime as dt
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import xlwt

from . import voucher_service as legacy

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES = ROOT / "data/domestic_voucher_rules_fit.json"
CONTRACT_FIELD = "合同号_1"
SOURCE_METHOD = "内销"
HISTORY_SUFFIXES = sorted({
    "内销收入",
    "销收入",
    "内采购商品收入-专票",
    "内采购商品收入",
    "内销采购商品收入-专票",
    "内销采购商品收入",
    "采购商品成本-专票",
    "采购商品成本",
    "结转内销采购成本",
    "结转采购成本",
}, key=len, reverse=True)

class DomesticVoucherError(Exception):
    pass


def text(value: Any) -> str:
    if isinstance(value, list):
        return "；".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in value
        ).strip()
    return str(value or "").strip()


def number(value: Any, default: Decimal | None = Decimal("0")) -> Decimal | None:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def money(value: Any) -> Decimal:
    return (number(value) or Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def mode(values) -> str:
    if isinstance(values, collections.Counter):
        return values.most_common(1)[0][0] if values else ""
    counts = collections.Counter(str(value) for value in values if str(value or "").strip())
    return counts.most_common(1)[0][0] if counts else ""


def aux_value(line: dict, label: str) -> dict:
    match = re.search(
        rf"【{re.escape(label)}:([^/】]+)/([^】]+)】",
        text(line.get("辅助核算")),
    )
    return {"code": match.group(1), "name": match.group(2)} if match else {}


def line_role(line: dict) -> str:
    code = text(line.get("科目编码"))
    summary = text(line.get("摘要"))
    debit = number(line.get("本币借方"), Decimal("0")) or Decimal("0")
    credit = number(line.get("本币贷方"), Decimal("0")) or Decimal("0")
    if aux_value(line, "客户"):
        return "receivable"
    if code.startswith("22210102") and credit:
        return "output_tax"
    if code.startswith("500102") and credit:
        return "revenue"
    if code.startswith("222101") and debit and ("专票" in summary or code in ("22210101", "22210107")):
        return "input_tax"
    if aux_value(line, "供应商"):
        return "payable"
    if aux_value(line, "存货档案"):
        return "inventory"
    if code.startswith("5401") and debit and "成本" in text(line.get("科目名称")) + summary:
        return "cost"
    return "other"


def normalize_name(value: Any) -> str:
    return legacy.normalize_name(text(value))


def resolve_name(mapping: dict, candidates, fallback=None) -> str:
    for raw in candidates:
        value = text(raw)
        if not value:
            continue
        variants = [value, *re.split(r"[,，;；/|]", value)]
        for variant in variants:
            code = mapping.get(normalize_name(variant))
            if code:
                return str(code)
        if fallback:
            code = fallback(value)
            if code:
                return str(code)
    return ""


def add_name_mapping(target: dict, name: str, code: str) -> None:
    key = normalize_name(name)
    if key and code:
        target[key] = str(code)


def _strip_contract(description: str, contracts: list[str]) -> tuple[bool, str]:
    value = description.replace("\xa0", " ").strip()
    pending = value.startswith("暂估-")
    if pending:
        value = value[len("暂估-"):]
    for contract in sorted(contracts, key=len, reverse=True):
        if value.startswith(contract):
            value = value[len(contract):].strip()
            break
    return pending, value


def contract_from_summary(description: Any) -> str:
    value = text(description).replace("\xa0", " ").strip()
    if value.startswith("暂估-"):
        value = value[len("暂估-"):]
    for suffix in HISTORY_SUFFIXES:
        if value.endswith(suffix):
            return value[:-len(suffix)].strip()
    return ""


def raw_line_role(line: dict) -> str:
    code = text(line.get("bodies_accsubjectVid_code"))
    fields = {
        text(item.get("fieldname"))
        for item in line.get("clientAuxiliaryDTOS") or []
    }
    if "客户" in fields:
        return "sales"
    if code.startswith("5001") or code.startswith("505") or code.startswith("22210102"):
        return "sales"
    if "供应商" in fields or code == "220201" or code.startswith("2221010"):
        return "purchase"
    if "存货档案" in fields or code.startswith("1405"):
        return "cost"
    if code.startswith("540"):
        return "cost"
    return "other"


def fit_merge_patterns(domestic_payload: dict, voucher_records: list[dict]) -> dict:
    """Classify how finance grouped sales, purchase and cost into vouchers.

    This is diagnostic by default: the leave-one-out accuracy is recorded so a
    weakly predictive policy is not silently used by generation.
    """
    source_groups = collections.defaultdict(list)
    for record in domestic_payload.get("records") or []:
        contract = text(record.get(CONTRACT_FIELD))
        if contract:
            source_groups[contract].append(record)
    history_groups = collections.defaultdict(list)
    for row in voucher_records:
        contract = contract_from_summary(row.get("bodydescription"))
        if contract:
            history_groups[contract].append(row)

    training = []
    pattern_count = collections.Counter()
    content_mode_count = collections.Counter()
    examples = collections.defaultdict(list)
    for contract in sorted(set(source_groups) & set(history_groups)):
        by_voucher = collections.defaultdict(list)
        for row in history_groups[contract]:
            by_voucher[text(row.get("id"))].append(row)
        shapes = []
        for rows in by_voucher.values():
            roles = {raw_line_role(row) for row in rows}
            label = ""
            if "sales" in roles:
                label += "S"
            if "purchase" in roles:
                label += "P"
            if "cost" in roles:
                label += "C"
            shapes.append(label)
        pattern = "+".join(sorted(shapes))
        records = source_groups[contract]
        signature = (
            contract[:4],
            len(records),
            len({text(record.get("供应商")) for record in records if text(record.get("供应商"))}),
            "+".join(sorted({text(record.get("开票情况")) for record in records})),
            "+".join(sorted({text(record.get("实际月份")) for record in records})),
        )
        content_mode = (
            "full" if "P" in pattern
            else "sales_cost" if "S" in pattern and "C" in pattern
            else "sales"
        )
        training.append({
            "contract": contract,
            "signature": signature,
            "pattern": pattern,
            "content_mode": content_mode,
        })
        pattern_count[pattern] += 1
        content_mode_count[content_mode] += 1
        if len(examples[pattern]) < 8:
            examples[pattern].append(contract)

    signature_counts = collections.defaultdict(collections.Counter)
    mode_signature_counts = collections.defaultdict(collections.Counter)
    for item in training:
        signature_counts[item["signature"]][item["pattern"]] += 1
        mode_signature_counts[item["signature"]][item["content_mode"]] += 1

    correct = 0
    mode_correct = 0
    for index, item in enumerate(training):
        candidates = training[:index] + training[index + 1:]
        def distance(other):
            expected = other["signature"]
            actual = item["signature"]
            return (
                4 * (actual[0] != expected[0])
                + 2 * (actual[2] != expected[2])
                + abs(actual[1] - expected[1])
                + 0.5 * (actual[3] != expected[3])
                + 0.25 * (actual[4] != expected[4])
            )
        nearest = min(candidates, key=distance) if candidates else item
        correct += nearest["pattern"] == item["pattern"]
        mode_correct += nearest["content_mode"] == item["content_mode"]

    return {
        "sample_contracts": len(training),
        "patterns": [
            {
                "pattern": pattern,
                "meaning": {
                    "SPC": "收入、采购、成本在同一张凭证",
                    "SC": "收入、成本在同一张凭证，无采购/进项",
                    "C+S": "收入一张凭证，成本另起一张凭证",
                    "S": "仅收入凭证",
                }.get(pattern, pattern),
                "count": count,
                "examples": examples[pattern],
            }
            for pattern, count in pattern_count.most_common()
        ],
        "content_modes": dict(content_mode_count),
        "content_mode_model": [
            {
                "signature": {
                    "prefix": signature[0],
                    "records": signature[1],
                    "suppliers": signature[2],
                    "invoice_status": signature[3],
                    "month": signature[4],
                },
                "mode": counter.most_common(1)[0][0],
                "counts": dict(counter),
                "confidence": round(counter.most_common(1)[0][1] / sum(counter.values()), 4),
                "support": sum(counter.values()),
            }
            for signature, counter in sorted(mode_signature_counts.items())
        ],
        "signature_counts": [
            {
                "prefix": signature[0],
                "records": signature[1],
                "suppliers": signature[2],
                "invoice_status": signature[3],
                "month": signature[4],
                "patterns": dict(counter),
                "predicted": counter.most_common(1)[0][0],
                "confidence": round(counter.most_common(1)[0][1] / sum(counter.values()), 4),
            }
            for signature, counter in sorted(signature_counts.items())
        ],
        "leave_one_out": {
            "correct": correct,
            "total": len(training),
            "accuracy": round(correct / len(training), 4) if training else 0,
            "reliable_for_automatic_generation": bool(
                training and correct / len(training) >= 0.9
            ),
        },
        "content_mode_leave_one_out": {
            "correct": mode_correct,
            "total": len(training),
            "accuracy": round(mode_correct / len(training), 4) if training else 0,
        },
    }


def fit_rules(sample_payload: dict, domestic_payload: dict | None = None,
              voucher_records: list[dict] | None = None) -> dict:
    """Infer reusable domestic voucher rules from downloaded Yonyou samples."""
    samples = [
        sample for sample in sample_payload.get("samples") or []
        if sample.get("类型") == "内销业务"
    ]
    contracts = set(sample_payload.get("matched_domestic_contracts") or [])
    source_groups: dict[str, list[dict]] = collections.defaultdict(list)
    if domestic_payload:
        for record in domestic_payload.get("records") or []:
            contract = text(record.get(CONTRACT_FIELD))
            if contract:
                source_groups[contract].append(record)
                contracts.add(contract)

    role_accounts = collections.defaultdict(collections.Counter)
    receivable_by_currency = collections.defaultdict(collections.Counter)
    inventory_subjects = collections.defaultdict(collections.Counter)
    pending_input = collections.Counter()
    normal_input = collections.Counter()
    customer_names = {}
    supplier_names = {}
    inventory_names = {}
    product_inventory = collections.defaultdict(collections.Counter)
    summary_counts = collections.defaultdict(collections.Counter)
    summary_by_year = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    order_counter = collections.Counter()
    maker_counts = collections.Counter()
    voucher_types = collections.Counter()
    quantity_fields = collections.Counter()
    purchase_order_counter = collections.Counter()
    purchase_order_by_year = collections.defaultdict(collections.Counter)
    purchase_order_by_prefix = collections.defaultdict(collections.Counter)
    voucher_seed_lines = 0

    for record in voucher_records or []:
        for auxiliary in record.get("clientAuxiliaryDTOS") or []:
            field_name = text(auxiliary.get("fieldname"))
            value = auxiliary.get("value") or {}
            code = text(value.get("code"))
            name = text(value.get("name"))
            if field_name == "客户":
                add_name_mapping(customer_names, name, code)
            elif field_name == "供应商":
                add_name_mapping(supplier_names, name, code)
            elif field_name == "存货档案":
                add_name_mapping(inventory_names, name, code)
                voucher_seed_lines += 1
    amount_tolerances = []

    contract_list = sorted(contracts, key=len, reverse=True)
    for sample in samples:
        maker_counts[text(sample.get("制单人"))] += 1
        descriptions = sample.get("摘要") or []
        voucher_types[text(sample.get("类型"))] += 1
        matched = sample.get("匹配合同号_1") or []
        records = []
        for contract in matched:
            records.extend(source_groups.get(contract, []))
        customer_codes = set()

        line_roles = [line_role(line) for line in sample.get("分录") or []]
        for index in range(len(line_roles) - 2):
            sequence = tuple(line_roles[index:index + 3])
            if set(sequence) == {"payable", "inventory", "input_tax"}:
                purchase_order_counter[sequence] += 1
                year = text(sample.get("期间"))[:4]
                purchase_order_by_year[year][sequence] += 1
                description = text((sample.get("分录") or [])[index].get("摘要"))
                match = re.search(r"(24|25|26)MT", description, re.IGNORECASE)
                if match:
                    purchase_order_by_prefix[year + match.group(1) + "MT"][sequence] += 1

        for line in sample.get("分录") or []:
            role = line_role(line)
            code = text(line.get("科目编码"))
            debit = number(line.get("本币借方"), Decimal("0")) or Decimal("0")
            if role != "other":
                role_accounts[role][code] += 1
            currency = text(line.get("币种")) or "人民币"
            if role == "receivable":
                receivable_by_currency[currency][code] += 1
            if role == "inventory":
                inventory = aux_value(line, "存货档案")
                if inventory:
                    add_name_mapping(inventory_names, inventory["name"], inventory["code"])
                    inventory_subjects[inventory["code"][:4]][code] += 1
            if role == "input_tax":
                pending = any(
                    text(description).startswith("暂估-")
                    for description in descriptions
                    if line.get("摘要") == description
                )
                (pending_input if pending else normal_input)[code] += 1

            customer = aux_value(line, "客户")
            if customer:
                add_name_mapping(customer_names, customer["name"], customer["code"])
                customer_codes.add(customer["code"])
            supplier = aux_value(line, "供应商")
            if supplier:
                add_name_mapping(supplier_names, supplier["name"], supplier["code"])

            if role in ("revenue", "payable", "cost") or (role == "inventory" and debit):
                pending, suffix = _strip_contract(text(line.get("摘要")), contract_list)
                if suffix.endswith("-专票"):
                    suffix = suffix[:-3]
                if suffix:
                    summary_counts[role][suffix] += 1
                    summary_by_year[text(sample.get("期间"))[:4]][role][suffix] += 1

            if role == "inventory" and debit:
                inventory = aux_value(line, "存货档案")
                line_cost = number(line.get("本币借方"), Decimal("0")) or Decimal("0")
                candidates = [
                    record for record in records
                    if abs((number(record.get("不含税成本"), Decimal("0")) or Decimal("0"))
                           - line_cost) <= Decimal("0.02")
                ]
                if inventory:
                    for record in candidates:
                        product = text(record.get("报关品名"))
                        if product:
                            product_inventory[product][inventory["code"]] += 1
                if line.get("数量") is not None:
                    for record in candidates:
                        for key, value in record.items():
                            try:
                                if abs(float(value) - float(line["数量"])) <= 0.011:
                                    quantity_fields[key] += 1
                            except (TypeError, ValueError):
                                pass
        if len(customer_codes) == 1:
            customer_code = next(iter(customer_codes))
            for record in records:
                for field in ("客户名称_脚本用", "买方英文名"):
                    add_name_mapping(customer_names, text(record.get(field)), customer_code)

        # Infer the modal ordering of purchase and cost blocks from 8/13-line patterns.
        for line in sample.get("分录") or []:
            role = line_role(line)
            if role in ("payable", "inventory", "input_tax", "cost"):
                order_counter[role] += 1

    normal_input_code = mode(normal_input) or "22210101"
    pending_input_code = mode(pending_input) or "22210107"
    inventory_prefix_map = {
        prefix: counts.most_common(1)[0][0]
        for prefix, counts in inventory_subjects.items()
        if counts
    }
    pattern_hits = sum(
        code == "1405" + prefix[-2:]
        for prefix, code in inventory_prefix_map.items()
    )
    inventory_pattern = (
        "1405{prefix_last2}"
        if inventory_prefix_map and pattern_hits / len(inventory_prefix_map) >= 0.8
        else ""
    )

    modern_summary_counts = summary_by_year.get("2026") or summary_counts
    purchase_summary = mode(modern_summary_counts["inventory"]) or "采购商品成本"
    if "采购商品成本" not in purchase_summary and "内采购商品收入" in purchase_summary:
        # Prefer the dominant modern wording when both historical styles exist.
        modern = [
            value for value, _ in summary_counts["inventory"].most_common()
            if "采购商品成本" in value
        ]
        if modern:
            purchase_summary = modern[0]
    if "内销" in purchase_summary and "采购商品成本" not in purchase_summary:
        purchase_summary = "采购商品成本"

    cost_summary_mode = mode(modern_summary_counts["cost"]) or "结转采购成本"
    if "结转采购成本" in cost_summary_mode:
        cost_summary = "结转采购成本"
    else:
        cost_summary = "结转采购成本"

    rules = {
        "version": 1,
        "fitted_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "training": {
            "domestic_vouchers": len(samples),
            "matched_contracts": len(contracts),
            "auxiliary_seed_lines": len(voucher_records or []),
            "maker": mode(maker_counts),
            "voucher_type": "记",
        },
        "accounts": {
            "receivable_by_currency": {
                currency: counts.most_common(1)[0][0]
                for currency, counts in receivable_by_currency.items()
            },
            "revenue": mode(role_accounts["revenue"]) or "50010201",
            "output_tax": mode(role_accounts["output_tax"]) or "22210102",
            "input_tax": normal_input_code,
            "pending_input_tax": pending_input_code,
            "payable": mode(role_accounts["payable"]) or "220201",
            "cost": mode(role_accounts["cost"]) or "540102",
            "inventory_by_prefix": inventory_prefix_map,
            "inventory_pattern": inventory_pattern,
            "inventory_fallback": inventory_prefix_map.get("0599") or "140599",
        },
        "summary": {
            "revenue": "内销收入",
            "purchase": purchase_summary,
            "cost": cost_summary,
            "input_tax_suffix": "-专票",
            "pending_prefix": "暂估-",
        },
        "purchase_order": list(
            (
                (purchase_order_by_year.get("2026") or purchase_order_counter)
                .most_common(1)[0][0]
            )
            if purchase_order_counter else ("payable", "inventory", "input_tax")
        ),
        "purchase_order_by_context": {
            context: list(counter.most_common(1)[0][0])
            for context, counter in purchase_order_by_prefix.items()
            if counter
        },
        "quantity_field": mode(quantity_fields) or "开票数量（实际）",
        "quantity_fallback": "报关重量",
        "mappings": {
            "customers": customer_names,
            "suppliers": supplier_names,
            "inventory_names": inventory_names,
            "product_to_inventory": {
                normalize_name(product): counts.most_common(1)[0][0]
                for product, counts in product_inventory.items()
                if counts
            },
        },
        "evidence": {
            "role_account_counts": {
                role: list(counter.items()) for role, counter in role_accounts.items()
            },
            "normal_input_accounts": list(normal_input.items()),
            "pending_input_accounts": list(pending_input.items()),
            "summary_counts": {
                role: list(counter.items()) for role, counter in summary_counts.items()
            },
            "quantity_fields": list(quantity_fields.items()),
            "purchase_order_totals": list(purchase_order_counter.items()),
            "purchase_order_by_year": {
                year: list(counter.items())
                for year, counter in purchase_order_by_year.items()
            },
            "purchase_order_by_prefix": {
                key: list(counter.items())
                for key, counter in purchase_order_by_prefix.items()
            },
        },
        "merge_fit": fit_merge_patterns(domestic_payload or {}, voucher_records or []),
    }
    return rules


def _customer_code(rules: dict, record: dict, masters: legacy.MasterData) -> str:
    return resolve_name(
        rules["mappings"]["customers"],
        [record.get("客户名称_脚本用"), record.get("买方英文名")],
        lambda value: masters.customer_code([value]),
    ) or resolve_name(
        rules["mappings"]["customers"],
        [record.get("买方英文名"), record.get("客户名称_脚本用")],
        lambda value: masters.customer_code([value]),
    )


def _supplier_code(rules: dict, record: dict, masters: legacy.MasterData) -> str:
    return resolve_name(
        rules["mappings"]["suppliers"],
        [record.get("供应商"), record.get("供应商简称")],
        lambda value: masters.supplier_code(value, ""),
    )


def _inventory_code(rules: dict, record: dict, masters: legacy.MasterData) -> str:
    product = text(record.get("报关品名"))
    variants = [
        product,
        product.replace("镍基", "镍合金"),
        product.replace("合金镍", "镍合金"),
        product.replace("镍合金焊管", "镍基焊管"),
    ]
    for candidate in dict.fromkeys(variants):
        code = (
            rules["mappings"]["product_to_inventory"].get(normalize_name(candidate))
            or rules["mappings"]["inventory_names"].get(normalize_name(candidate))
            or masters.inventory_code(candidate)
        )
        if code:
            return code
    return ""


def _quantity(record: dict, rules: dict) -> Decimal | None:
    for field in (rules.get("quantity_field"), rules.get("quantity_fallback")):
        value = number(record.get(field), None) if field else None
        if value is not None and value != 0:
            return value
    return None


def merge_signature(records: list[dict]) -> dict:
    contract = text(records[0].get(CONTRACT_FIELD))
    return {
        "prefix": contract[:4],
        "records": len(records),
        "suppliers": len({
            text(record.get("供应商"))
            for record in records
            if text(record.get("供应商"))
        }),
        "invoice_status": "+".join(sorted({
            text(record.get("开票情况")) for record in records
        })),
        "month": "+".join(sorted({
            text(record.get("实际月份")) for record in records
        })),
    }


def predict_merge_mode(rules: dict, records: list[dict]) -> tuple[str, float]:
    model = (rules.get("merge_fit") or {}).get("content_mode_model") or []
    if not model:
        return "full", 0.0
    signature = merge_signature(records)

    def distance(item):
        expected = item["signature"]
        return (
            4 * (signature["prefix"] != expected["prefix"])
            + 2 * (signature["suppliers"] != expected["suppliers"])
            + abs(signature["records"] - expected["records"])
            + 0.5 * (signature["invoice_status"] != expected["invoice_status"])
            + 0.25 * (signature["month"] != expected["month"])
        )

    nearest = min(model, key=distance)
    return nearest["mode"], float(nearest.get("confidence") or 0)


def _row(subject: str, summary: str, *, debit=Decimal("0"), credit=Decimal("0"),
         customer="", supplier="", inventory="", quantity=None, unit_price=None,
         currency="CNY", rate=Decimal("1")) -> dict:
    debit = money(debit)
    credit = money(credit)
    return {
        "科目编码": subject,
        "摘要": summary,
        "币种名称": currency,
        "汇率": float(rate),
        "原币借方": float(debit),
        "原币贷方": float(credit),
        "借方金额": float(debit),
        "贷方金额": float(credit),
        "客户编码": customer or "",
        "供应商编码": supplier or "",
        "存货档案": inventory or "",
        "单价": float(unit_price) if unit_price is not None else None,
        "借方数量": float(quantity) if quantity is not None and debit else None,
        "贷方数量": float(quantity) if quantity is not None and credit else None,
    }


def build_domestic_voucher(records: list[dict], rules: dict,
                           masters: legacy.MasterData | None = None) -> dict:
    if not records:
        raise DomesticVoucherError("空合同分组")
    masters = masters or legacy.MasterData()
    contract = text(records[0].get(CONTRACT_FIELD))
    if not contract:
        raise DomesticVoucherError("合同号_1 为空")
    customer = ""
    for record in records:
        customer = _customer_code(rules, record, masters)
        if customer:
            break
    if not customer:
        raise DomesticVoucherError("无法匹配客户档案: " + contract)

    numeric_required = ("报关金额", "不含税人民币收入", "销项税额")
    missing = [
        field for field in numeric_required
        if any(number(record.get(field), None) is None for record in records)
    ]
    if any(not text(record.get("币种")) for record in records):
        missing.append("币种")
    if missing:
        raise DomesticVoucherError("缺少收入字段: " + "、".join(missing))

    currencies = {text(record.get("币种")).upper() for record in records}
    currency = currencies.pop() if len(currencies) == 1 else "CNY"
    accounts = rules["accounts"]
    summaries = rules["summary"]
    receivable = accounts["receivable_by_currency"].get(currency)
    if not receivable:
        receivable = accounts["receivable_by_currency"].get("CNY", "112201")

    gross = money(sum(number(record.get("报关金额")) or Decimal("0") for record in records))
    revenue = money(sum(number(record.get("不含税人民币收入")) or Decimal("0") for record in records))
    output_tax = money(sum(number(record.get("销项税额")) or Decimal("0") for record in records))
    if abs(gross - revenue - output_tax) > Decimal("0.02"):
        raise DomesticVoucherError(
            f"收入区金额不平: 含税={gross}, 不含税={revenue}, 销项税={output_tax}"
        )

    income_summary = contract + summaries["revenue"]
    rows = [
        _row(receivable, income_summary, debit=gross, customer=customer, currency=currency),
        _row(accounts["revenue"], income_summary, credit=revenue, currency=currency),
        _row(accounts["output_tax"], income_summary, credit=output_tax),
    ]

    purchase_rows = []
    cost_blocks = []
    warnings = []
    for index, record in enumerate(records, start=1):
        cost = money(record.get("不含税成本"))
        purchase = money(record.get("采购金额"))
        input_tax = money(record.get("进项税额"))
        supplier = _supplier_code(rules, record, masters)
        inventory_code = _inventory_code(rules, record, masters)
        inventory_subject = ""
        if inventory_code:
            prefix = inventory_code[:4]
            inventory_subject = accounts["inventory_by_prefix"].get(prefix, "")
            pattern = accounts.get("inventory_pattern", "")
            if not inventory_subject and pattern:
                inventory_subject = pattern.format(prefix_last2=prefix[-2:])
            inventory_subject = inventory_subject or accounts["inventory_fallback"]

        if not supplier or not inventory_code or (cost <= 0 and purchase <= 0):
            warnings.append({
                "记录": index,
                "合同号_1": contract,
                "原因": "采购信息不完整，未生成该笔采购及成本分录",
                "供应商": text(record.get("供应商")),
                "报关品名": text(record.get("报关品名")),
            })
            continue
        if purchase <= 0:
            purchase = cost + input_tax

        pending = text(record.get("开票情况")) not in ("已开票", "已开", "开票完成")
        base_summary = (
            summaries["pending_prefix"] if pending else ""
        ) + contract + summaries["purchase"]
        tax_account = accounts["pending_input_tax"] if pending else accounts["input_tax"]
        quantity = _quantity(record, rules)
        unit_price = money(cost / quantity) if quantity else None

        purchase_rows.extend([
            _row(accounts["payable"], base_summary, credit=purchase, supplier=supplier),
            _row(inventory_subject, base_summary, debit=cost, inventory=inventory_code,
                 quantity=quantity, unit_price=unit_price),
            _row(tax_account, base_summary + summaries["input_tax_suffix"],
                 debit=input_tax),
        ])
        cost_blocks.extend([
            _row(accounts["cost"], contract + summaries["cost"], debit=cost),
            _row(inventory_subject, contract + summaries["cost"], credit=cost,
                 inventory=inventory_code, quantity=quantity, unit_price=unit_price),
        ])

    # Catalog this as a fitted ordering choice while retaining a deterministic fallback.
    role_rows = {
        "payable": [rows[0] for rows in zip(*[iter(purchase_rows)] * 3)],
        "inventory": [rows[1] for rows in zip(*[iter(purchase_rows)] * 3)],
        "input_tax": [rows[2] for rows in zip(*[iter(purchase_rows)] * 3)],
    }
    actual_year = text(records[0].get("实际年份")) or str(dt.date.today().year)
    prefix_match = re.match(r"^(\d{2}MT)", contract.upper())
    context = actual_year + (prefix_match.group(1) if prefix_match else "")
    configured_order = rules.get("purchase_order_by_context", {}).get(
        context, rules.get("purchase_order", [])
    )
    order = [role for role in configured_order if role in role_rows]
    if set(order) != {"payable", "inventory", "input_tax"}:
        order = ["payable", "inventory", "input_tax"]
    for index in range(len(role_rows["payable"])):
        for role in order:
            rows.append(role_rows[role][index])
    rows.extend(cost_blocks)

    merge_mode, merge_confidence = predict_merge_mode(rules, records)
    if merge_mode == "sales_cost":
        rows = [
            row for row in rows
            if text(row.get("摘要")).endswith("内销收入")
            or text(row.get("摘要")).endswith(summaries["cost"])
        ]
    elif merge_mode == "sales":
        rows = [
            row for row in rows
            if text(row.get("摘要")).endswith("内销收入")
        ]

    debit_total = money(sum(number(row["借方金额"]) or Decimal("0") for row in rows))
    credit_total = money(sum(number(row["贷方金额"]) or Decimal("0") for row in rows))
    if debit_total != credit_total:
        raise DomesticVoucherError(
            f"凭证借贷不平: 借方={debit_total}, 贷方={credit_total}"
        )
    return {
        "合同号_1": contract,
        "rows": rows,
        "record_ids": [record.get("record_id") for record in records],
        "warnings": warnings,
        "合并模式": merge_mode,
        "合并模式置信度": merge_confidence,
        "totals": {"debit": str(debit_total), "credit": str(credit_total)},
    }


def generate_all(domestic_payload: dict, rules: dict,
                 masters: legacy.MasterData | None = None) -> dict:
    masters = masters or legacy.MasterData()
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for record in domestic_payload.get("records") or []:
        if text(record.get("销售方式")) != SOURCE_METHOD:
            continue
        contract = text(record.get(CONTRACT_FIELD))
        groups[contract].append(record)

    generated = []
    failed = []
    skipped = []
    for contract, records in sorted(groups.items()):
        if not contract:
            skipped.append({
                "合同号_1": "",
                "记录数": len(records),
                "原因": "合同号_1 为空，无法确定凭证单位",
                "record_ids": [record.get("record_id") for record in records],
            })
            continue
        try:
            generated.append(build_domestic_voucher(records, rules, masters))
        except DomesticVoucherError as error:
            failed.append({
                "合同号_1": contract,
                "记录数": len(records),
                "原因": str(error),
            })
    return {
        "generated_count": len(generated),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "generated": generated,
        "failed": failed,
        "skipped": skipped,
    }


def write_excel(result: dict, path: Path, posting_date: dt.date,
                maker: str = "马云凤") -> None:
    book, sheet, columns = legacy._new_sheet(path)
    sheet._cell_overwrite_ok = True
    row_number = 1
    for voucher_id, voucher in enumerate(result["generated"], start=1):
        for row in voucher["rows"]:
            legacy._write_row(sheet, columns, row_number, row, voucher_id, posting_date)
            for field, value in (("制单人", maker), ("凭证类别", "记")):
                if field in columns:
                    sheet.write(row_number, columns[field], value)
            row_number += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    book.save(str(temporary))
    temporary.replace(path)


def write_single_excels(result: dict, directory: Path, posting_date: dt.date,
                        maker: str = "马云凤") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    index_rows = []
    for index, voucher in enumerate(result["generated"], start=1):
        contract = voucher["合同号_1"]
        safe_contract = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5&_-]", "_", contract)
        filename = f"{index:03d}_{safe_contract}.xls"
        path = directory / filename
        book, sheet, columns = legacy._new_sheet(path)
        sheet._cell_overwrite_ok = True
        for row_number, row in enumerate(voucher["rows"], start=1):
            legacy._write_row(sheet, columns, row_number, row, 1, posting_date)
            for field, value in (("制单人", maker), ("凭证类别", "记")):
                if field in columns:
                    sheet.write(row_number, columns[field], value)
        temporary = path.with_suffix(".part")
        book.save(str(temporary))
        temporary.replace(path)
        voucher["file"] = filename
        index_rows.append([
            index,
            contract,
            filename,
            len(voucher["rows"]),
            voucher.get("合并模式", ""),
            voucher.get("合并模式置信度", ""),
            len(voucher["warnings"]),
            "；".join(sorted({warning["原因"] for warning in voucher["warnings"]})),
        ])

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "单张凭证索引"
    headers = [
        "序号", "合同号_1", "导入文件", "分录行数",
        "合并模式", "模式置信度", "警告数", "警告原因",
    ]
    sheet.append(headers)
    for row in index_rows:
        sheet.append(row)
    for cell in sheet[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, header in enumerate(headers, start=1):
        widths = [len(header) * 2]
        widths.extend(min(50, len(str(row[index - 1] or "")) + 2) for row in index_rows)
        sheet.column_dimensions[get_column_letter(index)].width = max(12, max(widths))
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    index_path = directory / "单张凭证索引.xlsx"
    workbook.save(index_path)
    return index_path


def evaluate(generated: dict, sample_payload: dict) -> dict:
    """Independent post-generation comparison; never called by generation."""
    sample_by_contract = collections.defaultdict(list)
    for sample in sample_payload.get("samples") or []:
        if sample.get("类型") != "内销业务":
            continue
        for contract in sample.get("匹配合同号_1") or []:
            sample_by_contract[contract].append(sample)

    comparisons = []
    skipped_without_contract_lines = []
    suffixes = sorted({
        "内销收入",
        "销收入",
        "内采购商品收入-专票",
        "内采购商品收入",
        "内销采购商品收入-专票",
        "内销采购商品收入",
        "采购商品成本-专票",
        "采购商品成本",
        "结转内销采购成本",
        "结转采购成本",
    }, key=len, reverse=True)

    def summary_contract(description: str) -> str:
        value = description.replace("\xa0", " ").strip()
        if value.startswith("暂估-"):
            value = value[len("暂估-"):]
        for suffix in suffixes:
            if value.endswith(suffix):
                return value[:-len(suffix)].strip()
        return ""

    for voucher in generated.get("generated") or []:
        contract = voucher["合同号_1"]
        samples = sample_by_contract.get(contract, [])
        if not samples:
            continue
        generated_revenue = money(sum(
            number(row["贷方金额"]) or Decimal("0")
            for row in voucher["rows"]
            if text(row["科目编码"]).startswith("500102")
        ))
        sample_lines = []
        for sample in samples:
            for line in sample.get("分录") or []:
                description = text(line.get("摘要"))
                if summary_contract(description) == contract:
                    sample_lines.append(line)
        if not sample_lines:
            skipped_without_contract_lines.append(contract)
            continue
        generated_gross = money(sum(
            number(row["借方金额"]) or Decimal("0")
            for row in voucher["rows"]
            if text(row["摘要"]).endswith("内销收入")
        ))
        sample_gross = money(sum(
            number(line.get("本币借方")) or Decimal("0")
            for line in sample_lines
            if text(line.get("摘要")).endswith("内销收入")
        ))
        generated_tax = money(sum(
            number(row["贷方金额"]) or Decimal("0")
            for row in voucher["rows"]
            if text(row["科目编码"]).startswith("22210102")
        ))
        sample_tax = money(sum(
            number(line.get("本币贷方")) or Decimal("0")
            for line in sample_lines
            if text(line.get("科目编码")).startswith("22210102")
        ))
        sample_revenue = money(sum(
            number(line.get("本币贷方")) or Decimal("0")
            for line in sample_lines
            if text(line.get("科目编码")).startswith("500102")
        ))
        comparisons.append({
            "合同号_1": contract,
            "导入文件": voucher.get("file", ""),
            "生成不含税收入": str(generated_revenue),
            "历史不含税收入": str(sample_revenue),
            "差异": str(generated_revenue - sample_revenue),
            "生成含税收入": str(generated_gross),
            "历史含税收入": str(sample_gross),
            "生成销项税": str(generated_tax),
            "历史销项税": str(sample_tax),
            "收入一致": abs(generated_revenue - sample_revenue) <= Decimal("0.02"),
            "含税收入一致": abs(generated_gross - sample_gross) <= Decimal("0.02"),
            "销项税一致": abs(generated_tax - sample_tax) <= Decimal("0.02"),
            "一致": (
                abs(generated_revenue - sample_revenue) <= Decimal("0.02")
                and abs(generated_gross - sample_gross) <= Decimal("0.02")
                and abs(generated_tax - sample_tax) <= Decimal("0.02")
            ),
        })
    return {
        "compared_contracts": len(comparisons),
        "exact_revenue_matches": sum(item["收入一致"] for item in comparisons),
        "exact_sales_matches": sum(item["一致"] for item in comparisons),
        "skipped_without_contract_lines": skipped_without_contract_lines,
        "comparisons": comparisons,
    }


def evaluate_full_lines(generated: dict, sample_payload: dict,
                        voucher_records: list[dict] | None = None) -> dict:
    """Compare every accounting field of comparable generated/manual vouchers."""
    suffixes = sorted({
        "内销收入",
        "销收入",
        "内采购商品收入-专票",
        "内采购商品收入",
        "内销采购商品收入-专票",
        "内销采购商品收入",
        "采购商品成本-专票",
        "采购商品成本",
        "结转内销采购成本",
        "结转采购成本",
    }, key=len, reverse=True)

    def summary_contract(value: str) -> str:
        description = text(value).replace("\xa0", " ").strip()
        if description.startswith("暂估-"):
            description = description[len("暂估-"):]
        for suffix in suffixes:
            if description.endswith(suffix):
                return description[:-len(suffix)].strip()
        return ""

    def canonical(line: dict, sample: bool) -> dict:
        if sample:
            if line.get("科目编码") or line.get("辅助核算") is not None:
                debit = number(line.get("本币借方"), Decimal("0")) or Decimal("0")
                credit = number(line.get("本币贷方"), Decimal("0")) or Decimal("0")
                customer = aux_value(line, "客户").get("code", "")
                supplier = aux_value(line, "供应商").get("code", "")
                inventory = aux_value(line, "存货档案").get("code", "")
                quantity = line.get("数量")
                unit_price = line.get("单价")
                summary = text(line.get("摘要"))
                code = text(line.get("科目编码"))
            else:
                debit = number(line.get("debit_org"), Decimal("0")) or Decimal("0")
                credit = number(line.get("credit_org"), Decimal("0")) or Decimal("0")
                auxiliaries = {
                    text(item.get("fieldname")): text((item.get("value") or {}).get("code"))
                    for item in line.get("clientAuxiliaryDTOS") or []
                }
                customer = auxiliaries.get("客户", "")
                supplier = auxiliaries.get("供应商", "")
                inventory = auxiliaries.get("存货档案", "")
                quantity = line.get("quantity")
                unit_price = line.get("price")
                summary = text(line.get("bodydescription"))
                code = text(line.get("bodies_accsubjectVid_code"))
        else:
            code = text(line.get("科目编码"))
            debit = number(line.get("借方金额"), Decimal("0")) or Decimal("0")
            credit = number(line.get("贷方金额"), Decimal("0")) or Decimal("0")
            customer = text(line.get("客户编码"))
            supplier = text(line.get("供应商编码"))
            inventory = text(line.get("存货档案"))
            quantity = line.get("借方数量") or line.get("贷方数量")
            unit_price = line.get("单价")
            summary = text(line.get("摘要"))
        return {
            "科目编码": code,
            "借方": str(money(debit)),
            "贷方": str(money(credit)),
            "客户编码": customer,
            "供应商编码": supplier,
            "存货档案": inventory,
            "数量": str(number(quantity, None)) if quantity not in (None, "") else "",
            "单价": str(money(unit_price)) if unit_price not in (None, "") else "",
            "摘要": summary,
        }

    def without_summary(line: dict) -> tuple:
        return tuple((key, line[key]) for key in line if key != "摘要")

    def relaxed_line(line: dict) -> tuple:
        values = dict(without_summary(line))
        if values["科目编码"] in ("22210101", "22210107"):
            values["科目编码"] = "22210101/22210107"
        return tuple(sorted(values.items()))

    def account_totals(lines: list[dict]) -> dict:
        totals = collections.defaultdict(lambda: {"借方": Decimal("0"), "贷方": Decimal("0")})
        for line in lines:
            totals[line["科目编码"]]["借方"] += Decimal(line["借方"])
            totals[line["科目编码"]]["贷方"] += Decimal(line["贷方"])
        return {
            account: {
                "借方": str(money(values["借方"])),
                "贷方": str(money(values["贷方"])),
            }
            for account, values in totals.items()
        }

    def dimension_totals(lines: list[dict], include_aux: bool) -> dict:
        totals = collections.defaultdict(Decimal)
        for line in lines:
            debit = Decimal(line["借方"])
            credit = Decimal(line["贷方"])
            account = line["科目编码"]
            if account in ("22210101", "22210107"):
                account = "22210101/22210107"
            prefixes = [account, "借" if debit else "贷"]
            if include_aux:
                prefixes.extend([
                    line["客户编码"], line["供应商编码"], line["存货档案"],
                ])
            totals[tuple(prefixes)] += debit or credit
        return dict(totals)

    sample_by_contract = collections.defaultdict(list)
    voucher_ids_by_contract = collections.defaultdict(set)
    if voucher_records:
        for record in voucher_records:
            contract = summary_contract(record.get("bodydescription"))
            if contract:
                sample_by_contract[contract].append(record)
                voucher_ids_by_contract[contract].add(text(record.get("id")))
    else:
        for sample in sample_payload.get("samples") or []:
            if sample.get("类型") != "内销业务":
                continue
            for line in sample.get("分录") or []:
                contract = summary_contract(line.get("摘要"))
                if contract:
                    sample_by_contract[contract].append(line)
                    voucher_ids_by_contract[contract].add(text(sample.get("凭证ID")))

    comparisons = []
    for voucher in generated.get("generated") or []:
        contract = voucher["合同号_1"]
        historical = sample_by_contract.get(contract, [])
        if not historical:
            continue
        actual = [canonical(line, False) for line in voucher["rows"]]
        expected = [canonical(line, True) for line in historical]
        actual_without_summary = collections.Counter(without_summary(line) for line in actual)
        expected_without_summary = collections.Counter(without_summary(line) for line in expected)
        actual_relaxed = collections.Counter(relaxed_line(line) for line in actual)
        expected_relaxed = collections.Counter(relaxed_line(line) for line in expected)
        actual_full = collections.Counter(tuple(line.items()) for line in actual)
        expected_full = collections.Counter(tuple(line.items()) for line in expected)
        actual_accounts = account_totals(actual)
        expected_accounts = account_totals(expected)
        actual_dimensions = dimension_totals(actual, True)
        expected_dimensions = dimension_totals(expected, True)
        actual_account_groups = dimension_totals(actual, False)
        expected_account_groups = dimension_totals(expected, False)
        exact_common = sum((actual_without_summary & expected_without_summary).values())
        dimension_common = sum(
            1
            for key, value in actual_dimensions.items()
            if expected_dimensions.get(key) == value
        )
        account_common = sum(
            1
            for key, value in actual_account_groups.items()
            if expected_account_groups.get(key) == value
        )
        account_differences = []
        for account in sorted(set(actual_accounts) | set(expected_accounts)):
            generated_value = actual_accounts.get(account, {"借方": "0.00", "贷方": "0.00"})
            expected_value = expected_accounts.get(account, {"借方": "0.00", "贷方": "0.00"})
            if generated_value != expected_value:
                account_differences.append({
                    "科目编码": account,
                    "生成借方": generated_value["借方"],
                    "历史借方": expected_value["借方"],
                    "生成贷方": generated_value["贷方"],
                    "历史贷方": expected_value["贷方"],
                })
        relaxed = actual_relaxed == expected_relaxed
        reasons = []
        if not relaxed:
            if len(actual) != len(expected):
                reasons.append(f"分录数不同：生成 {len(actual)} 行，历史 {len(expected)} 行")
            actual_core = collections.Counter(
                (line["科目编码"], line["借方"], line["贷方"])
                if line["科目编码"] not in ("22210101", "22210107")
                else ("22210101/22210107", line["借方"], line["贷方"])
                for line in actual
            )
            expected_core = collections.Counter(
                (line["科目编码"], line["借方"], line["贷方"])
                if line["科目编码"] not in ("22210101", "22210107")
                else ("22210101/22210107", line["借方"], line["贷方"])
                for line in expected
            )
            if actual_core == expected_core:
                reasons.append("科目金额相同，但辅助核算、数量或单价不同")
            else:
                actual_accounts_relaxed = collections.defaultdict(Decimal)
                expected_accounts_relaxed = collections.defaultdict(Decimal)
                for line in actual:
                    account = (
                        "22210101/22210107"
                        if line["科目编码"] in ("22210101", "22210107")
                        else line["科目编码"]
                    )
                    actual_accounts_relaxed[account] += Decimal(line["借方"]) - Decimal(line["贷方"])
                for line in expected:
                    account = (
                        "22210101/22210107"
                        if line["科目编码"] in ("22210101", "22210107")
                        else line["科目编码"]
                    )
                    expected_accounts_relaxed[account] += Decimal(line["借方"]) - Decimal(line["贷方"])
                differing = [
                    account for account in sorted(set(actual_accounts_relaxed) | set(expected_accounts_relaxed))
                    if actual_accounts_relaxed[account] != expected_accounts_relaxed[account]
                ]
                if differing:
                    reasons.append("科目净额不同：" + "、".join(differing))
                else:
                    reasons.append("借方/贷方拆分不同")
        comparisons.append({
            "合同号_1": contract,
            "生成行数": len(actual),
            "历史行数": len(expected),
            "历史凭证ID": sorted(voucher_ids_by_contract[contract]),
            "会计字段一致": actual_without_summary == expected_without_summary,
            "忽略顺序及暂估进项科目后一致": relaxed,
            "剩余不一致原因": reasons,
            "科目汇总一致": not account_differences,
            "科目汇总差异": account_differences,
            "行顺序一致": actual == expected,
            "包含摘要完全一致": actual_full == expected_full,
            "完全一致分录数": exact_common,
            "同科目借方/贷方及辅助核算汇总匹配数": dimension_common,
            "同科目借贷总额匹配数": account_common,
            "宽松判定正确": bool(exact_common or dimension_common or account_common),
            "生成多出行": [
                dict(line) for line, count in (actual_without_summary - expected_without_summary).items()
                for _ in range(count)
            ],
            "历史多出行": [
                dict(line) for line, count in (expected_without_summary - actual_without_summary).items()
                for _ in range(count)
            ],
        })
    generated_contracts = {
        item["合同号_1"] for item in generated.get("generated") or []
    }
    not_made_contracts = sorted(generated_contracts - set(sample_by_contract))
    return {
        "compared_contracts": len(comparisons),
        "exact_accounting_matches": sum(item["会计字段一致"] for item in comparisons),
        "relaxed_matches": sum(item["忽略顺序及暂估进项科目后一致"] for item in comparisons),
        "exact_account_total_matches": sum(item["科目汇总一致"] for item in comparisons),
        "exact_ordered_matches": sum(item["行顺序一致"] for item in comparisons),
        "exact_full_matches": sum(item["包含摘要完全一致"] for item in comparisons),
        "loose_matched_contracts": sum(item["宽松判定正确"] for item in comparisons),
        "loose_mismatched_contracts": sum(
            not item["宽松判定正确"] for item in comparisons
        ),
        "not_made_or_not_found_contracts": not_made_contracts,
        "comparisons": comparisons,
    }


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_reconciliation_workbook(report: dict, path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    full = report["full_line_comparison"]
    workbook = Workbook()
    workbook.remove(workbook.active)

    def add_sheet(title: str, headers: list[str], rows: list[list[Any]]) -> None:
        target = workbook.create_sheet(title)
        target.append(headers)
        for row in rows:
            target.append(row)
        for cell in target[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = PatternFill("solid", fgColor="1F4E78")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        target.freeze_panes = "A2"
        target.auto_filter.ref = target.dimensions
        for index, header in enumerate(headers, start=1):
            widths = [len(header) * 2]
            widths.extend(
                min(50, len(str(row[index - 1] or "")) + 2)
                for row in rows[:200]
            )
            target.column_dimensions[get_column_letter(index)].width = max(12, max(widths))
        for row in target.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)

    summary_rows = []
    generated_extra = []
    historical_extra = []
    account_differences = []
    for item in full.get("comparisons") or []:
        summary_rows.append([
            item["合同号_1"],
            item.get("导入文件", ""),
            "是" if item["会计字段一致"] else "否",
            "是" if item["忽略顺序及暂估进项科目后一致"] else "否",
            "；".join(item["剩余不一致原因"]),
            "是" if item["科目汇总一致"] else "否",
            "是" if item["行顺序一致"] else "否",
            "是" if item["包含摘要完全一致"] else "否",
            "是" if item["宽松判定正确"] else "否",
            item["完全一致分录数"],
            item["同科目借方/贷方及辅助核算汇总匹配数"],
            item["同科目借贷总额匹配数"],
            item["生成行数"],
            item["历史行数"],
            "；".join(item["历史凭证ID"]),
        ])
        for line in item["生成多出行"]:
            generated_extra.append([
                item["合同号_1"], line["科目编码"], line["借方"], line["贷方"],
                line["客户编码"], line["供应商编码"], line["存货档案"],
                line["数量"], line["单价"],
            ])
        for line in item["历史多出行"]:
            historical_extra.append([
                item["合同号_1"], line["科目编码"], line["借方"], line["贷方"],
                line["客户编码"], line["供应商编码"], line["存货档案"],
                line["数量"], line["单价"],
            ])
        for difference in item["科目汇总差异"]:
            account_differences.append([
                item["合同号_1"], difference["科目编码"],
                difference["生成借方"], difference["历史借方"],
                difference["生成贷方"], difference["历史贷方"],
            ])

    add_sheet("差异汇总", [
        "合同号_1", "导入文件", "严格会计字段一致", "忽略顺序及暂估进项科目后一致",
        "剩余不一致原因", "科目汇总一致", "行顺序一致",
        "包含摘要完全一致", "宽松判定正确", "完全一致分录数",
        "维度汇总匹配数", "科目汇总匹配数", "生成行数", "历史行数", "历史凭证ID",
    ], summary_rows)
    add_sheet("生成多出行", [
        "合同号_1", "科目编码", "借方", "贷方", "客户编码",
        "供应商编码", "存货档案", "数量", "单价",
    ], generated_extra)
    add_sheet("历史多出行", [
        "合同号_1", "科目编码", "借方", "贷方", "客户编码",
        "供应商编码", "存货档案", "数量", "单价",
    ], historical_extra)
    add_sheet("科目汇总差异", [
        "合同号_1", "科目编码", "生成借方", "历史借方", "生成贷方", "历史贷方",
    ], account_differences)

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    workbook.save(temporary)
    temporary.replace(path)
