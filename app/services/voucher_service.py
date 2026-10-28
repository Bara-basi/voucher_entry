"""凭证构建服务：飞书记录 → 用友导入模板（53 列 .xls）。

规则来源：影刀旧版程序 `caluate.py`（已验证可行），并按财务确认做了三处对齐：
1. 存货科目以用友存货档案为准，找不到品名报异常；
2. 进项税额 / 出口退税行摘要带 `-专票`，销项税额不带；
3. 汇率取海关AI「汇率」表，按出口日期所在月份。
"""
from __future__ import annotations

import collections
import datetime
import json
import re
import unicodedata
from pathlib import Path

import xlrd
import xlwt

from app.settings import voucher_maker

ROOT = Path(__file__).resolve().parents[2]

TEMPLATE_PATH = ROOT / "templates/024_存货档案_反方向数量留空.xls"
INVENTORY_PATH = ROOT / "mappings/platform/存货档案.json"
CUSTOMER_PATH = ROOT / "mappings/platform/客户档案.json"
SUPPLIER_PATH = ROOT / "mappings/platform/供应商档案.json"
CUSTOMER_ALIAS_PATH = ROOT / "mappings/customer_alias_mappings.json"
CUSTOMER_HISTORY_PATH = ROOT / "mappings/customer_resolution_history.json"
SUPPLIER_ALIAS_PATH = ROOT / "mappings/finance_supplier_alias_mappings.json"
CUSTOMER_USAGE_PATH = ROOT / "mappings/customer_usage.json"
RATE_PATH = ROOT / "data/fx_rates_ai_snapshot.json"
AUTO_CUSTOMER_BASIS = "内销规则与睿贝买方映射自动同步"

EXCLUDED_MAKERS = {"王益"}          # 王益 = 导入测试样例
SCOPE_SALE_METHOD = "出口"
SCOPE_CONTRACT_PREFIX = "26MT"

# 影刀 caluate.py 的产品类别映射
PRODUCT_CODE_MAPPING = {
    "0501": "不锈钢产品", "0502": "镍合金产品", "0503": "阀门产品", "0504": "机械产品",
    "0505": "钛产品", "0506": "钢产品", "0507": "金属制品", "0508": "附件", "0599": "其他产品",
}

# 产品类别 -> 存货类科目后两位
INVENTORY_SUBJECT = {
    "不锈钢产品": "140501", "镍合金产品": "140502", "阀门产品": "140503",
    "机械产品": "140504", "钛产品": "140505", "钢产品": "140506",
    "金属制品": "140507", "附件": "140508", "其他产品": "140599",
}

# 销售方式 × 币种 -> (应收账款, 主营业务收入)
ACCOUNT_BY_METHOD = {
    "出口": {"USD": ("112202", "50010102"), "CNY": ("112201", "50010101"),
             "EUR": ("112203", "50010103"), "GBP": ("112205", "50010104"),
             "JPY": ("112207", "50010105")},
    "内销": {"USD": ("112202", "50010201"), "CNY": ("112201", "50010201"),
             "EUR": ("112203", "50010201"), "GBP": ("112205", "50010201"),
             "JPY": ("112207", "50010201")},
}

REQUIRED_FIELDS = ["报关品名", "供应商", "报关金额", "合同号_1", "销售方式", "币种",
                   "不含税人民币收入", "不含税成本", "采购金额"]


class VoucherError(Exception):
    """单组无法生成凭证（缺字段、档案缺失等），整组跳过。"""


def normalize_name(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(c for c in text if c.isalnum() and not unicodedata.combining(c))


def to_float(value, default=0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_text(value) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            parts.append(to_text(item))
        return " ".join(str(p) for p in parts if p).strip()
    if isinstance(value, dict):
        if value.get("text") is not None:
            return str(value["text"]).strip()
        if value.get("name") is not None:
            return str(value["name"]).strip()
        if value.get("value") is not None:
            return to_text(value["value"])
        return ""
    return str(value or "").strip()


def uses_export_customer(record) -> bool:
    """Finance may book numbered export orders using the domestic voucher template."""
    if to_text(record.get("销售方式")) != "内销":
        return True
    contract = re.sub(r"\s+", "", to_text(record.get("合同号_1")).upper()).removeprefix("PI-")
    return bool(re.match(r"^\d{2}MT-(?:0[1-9]|[1-9]\d)[A-Z]", contract))


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class MasterData:
    """客户 / 供应商 / 存货 / 汇率档案。"""

    def __init__(self):
        self.inventory = self._load_inventory()
        # Workbench-maintained overrides are separate from the read-only platform archive.
        product_alias_path = ROOT / "mappings/product_alias_mappings.json"
        if product_alias_path.exists():
            for item in read_json(product_alias_path).get("mappings", []):
                if item.get("enabled", True):
                    self.inventory[item["alias"]] = str(item["platform_code"])
                else:
                    self.inventory.pop(item["alias"], None)
        self.customers = read_json(CUSTOMER_PATH)["records"]
        self.suppliers = read_json(SUPPLIER_PATH)["records"]
        self.customer_aliases = {normalize_name(a["alias"]): a["platform_code"]
                                 for a in read_json(CUSTOMER_ALIAS_PATH)["aliases"]
                                 if a.get("enabled", True)
                                 and a.get("basis") != AUTO_CUSTOMER_BASIS}
        self.customer_history = {}
        for record in read_json(CUSTOMER_HISTORY_PATH)["records"]:
            self.customer_history.setdefault(record["normalized_alias"], record["platform_code"])
        self.supplier_aliases = {m["alias"]: m["platform_code"]
                                 for m in read_json(SUPPLIER_ALIAS_PATH)["mappings"]}
        self.supplier_by_name = {normalize_name(s["name"]): s["code"] for s in self.suppliers}
        self.customer_by_name = collections.defaultdict(set)
        for customer in self.customers:
            self.customer_by_name[normalize_name(customer["name"])].add(customer["code"])
        self.customer_shortname = {str(c["code"]): str(c.get("shortname") or "")
                                   for c in self.customers}
        self.customer_usage = (read_json(CUSTOMER_USAGE_PATH)
                               if CUSTOMER_USAGE_PATH.exists() else {})
        self.rates = self._load_rates()

    @staticmethod
    def _load_inventory():
        buckets = collections.defaultdict(list)
        for record in read_json(INVENTORY_PATH)["records"]:
            if not record.get("isEnd"):
                continue
            name = str(record.get("name") or "").strip()
            if name:
                buckets[name].append(str(record.get("code") or ""))
        result = {}
        for name, codes in buckets.items():
            # 同名多条时，按影刀填单逻辑优先取以 05 开头的存货编码
            result[name] = next((c for c in codes if c.startswith("05")), codes[0])
        return result

    @staticmethod
    def _load_rates():
        table = {}
        for record in read_json(RATE_PATH)["records"]:
            fields = record["fields"]
            currency = fields.get("币种")
            if isinstance(currency, list):
                currency = currency[0] if currency else None
            try:
                table[(str(fields.get("实际年份")), str(fields.get("实际月份")),
                       str(currency).upper())] = float(fields.get("汇率"))
            except (TypeError, ValueError):
                continue
        return table

    def rate(self, export_ts, currency: str) -> float | None:
        if currency.upper() in ("CNY", "RMB"):
            return 1.0
        if not isinstance(export_ts, (int, float)):
            return None
        moment = datetime.datetime.fromtimestamp(export_ts / 1000)
        return self.rates.get((str(moment.year), str(moment.month), currency.upper()))

    def inventory_code(self, product_name: str) -> str | None:
        return self.inventory.get((product_name or "").strip())

    def customer_code(self, names, *, use_history=True) -> str | None:
        for raw in names:
            name = to_text(raw)
            if not name:
                continue
            normalized = normalize_name(name)
            code = self.customer_aliases.get(normalized)
            if code:
                return code
            candidates = set(self.customer_by_name.get(normalized, set()))
            history = self.customer_history.get(normalized) if use_history else None
            if history in candidates:
                return history
            # An exact archive name must not be mixed with an unrelated historical guess.
            if not candidates and history and history in self.customer_shortname:
                candidates.add(history)
            if not candidates:
                # 档案全名常带括号补充说明（如 TOROCA(SECIME...)），允许唯一前缀匹配
                key = normalized.replace("and", "")
                if len(key) >= 6:
                    for archive_key, codes in self.customer_by_name.items():
                        other = archive_key.replace("and", "")
                        if len(other) >= 6 and (other.startswith(key) or key.startswith(other)):
                            candidates |= codes
            if candidates:
                # 同一简称（如 3-095）的档案是同一客户的不同版本，一并纳入比较
                shortnames = {self.customer_shortname.get(c, "") for c in candidates}
                shortnames.discard("")
                if len(shortnames) == 1:
                    only = next(iter(shortnames))
                    candidates |= {c for c, sn in self.customer_shortname.items() if sn == only}
            if len(candidates) == 1:
                return next(iter(candidates))
            if len(candidates) > 1:
                shortnames = {self.customer_shortname.get(c, "") for c in candidates}
                if len(shortnames) == 1 and "" not in shortnames:
                    usage = self.customer_usage
                    ranked = sorted(candidates, key=lambda c: (-int(usage.get(c, 0)), c))
                    return ranked[0]
                # Different customers must not be resolved by code format or row order.
                return None
        return None

    def customer_for_record(self, record) -> str | None:
        fields = (("客户名称_脚本用", "买方英文名") if to_text(record.get("销售方式")) == "内销"
                  else ("买方英文名", "客户名称_脚本用"))
        # The second field is only a fallback when the primary source is empty.
        name = next((to_text(record.get(f)) for f in fields if to_text(record.get(f))), "")
        return self.customer_code([name], use_history=uses_export_customer(record)) if name else None

    def customer_for_records(self, records) -> str | None:
        codes = set()
        for record in records:
            if not any(to_text(record.get(f)) for f in ("买方英文名", "客户名称_脚本用")):
                continue
            code = self.customer_for_record(record)
            if not code:
                return None
            codes.add(code)
        return next(iter(codes)) if len(codes) == 1 else None

    def supplier_code(self, short_name: str, full_name: str = "") -> str | None:
        short_name = to_text(short_name)
        if short_name in self.supplier_aliases:
            return self.supplier_aliases[short_name]
        normalized = normalize_name(to_text(full_name) or short_name)
        if not normalized:
            return None
        return self.supplier_by_name.get(normalized)


MASTERS: MasterData | None = None


def get_masters() -> MasterData:
    global MASTERS
    if MASTERS is None:
        MASTERS = MasterData()
    return MASTERS


def build_voucher(records, masters: MasterData | None = None) -> list[dict]:
    """同一报关单号下的记录 → 一张凭证的分录行。"""
    masters = masters or get_masters()
    if not records:
        raise VoucherError("空分组")

    for field in REQUIRED_FIELDS:
        if any(to_text(record.get(field)) == "" for record in records):
            raise VoucherError("缺失字段: " + field)

    method = to_text(records[0].get("销售方式"))
    if method not in ACCOUNT_BY_METHOD:
        method = "内销"
    currency = to_text(records[0].get("币种")).upper()
    contract = to_text(records[0].get("合同号_1"))
    account_map = ACCOUNT_BY_METHOD[method]
    receivable, revenue = account_map.get(currency, account_map["CNY"])
    cost_account = "540101" if method == "出口" else "540102"
    freight_account = "56010802" if method == "出口" else "56010801"

    rate = masters.rate(records[0].get("出口日期"), currency)
    if rate is None:
        rate = to_float(records[0].get("汇率"), default=0.0)
    if not rate:
        raise VoucherError("缺少汇率: %s %s" % (currency, records[0].get("出口日期")))

    total_export = sum(to_float(r.get("报关金额")) for r in records)
    total_tax = sum(to_float(r.get("销项税额")) for r in records)
    total_revenue = sum(to_float(r.get("不含税人民币收入")) for r in records)
    total_freight = sum(to_float(r.get("运费")) for r in records)
    total_insurance = sum(to_float(r.get("保费")) for r in records)

    customer = masters.customer_for_records(records)
    if not customer:
        raise VoucherError("无法匹配客户")

    rows: list[dict] = []

    def add(summary, subject, aux_customer=None, aux_supplier=None, aux_inventory=None,
            currency_code="CNY", original=0.0, local=0.0, debit=True,
            quantity=None, unit_price=None):
        rows.append({
            "科目编码": subject, "摘要": summary, "币种名称": currency_code,
            "汇率": rate if currency_code != "CNY" else 1.0,
            "原币借方": round(original, 2) if debit else 0.0,
            "原币贷方": 0.0 if debit else round(original, 2),
            "借方金额": round(local, 2) if debit else 0.0,
            "贷方金额": 0.0 if debit else round(local, 2),
            "客户编码": aux_customer or "",
            "供应商编码": aux_supplier or "",
            "存货档案": aux_inventory or "",
            "单价": unit_price,
            "借方数量": quantity if debit and quantity else None,
            "贷方数量": None if debit else (quantity if quantity else None),
        })

    income_summary = contract + ("外销收入" if method == "出口" else "内销收入")
    # 应收行本币由收入区其余各行倒算，保证账簿币借贷分毫不差：
    # 应收 = 不含税收入 + 销项税额 + |运费| + |保费|
    revenue_local = round(total_revenue, 2)
    tax_local = round(total_tax, 2)
    freight_local = round(-total_freight * rate, 2)
    insurance_local = round(-total_insurance * rate, 2)
    receivable_local = round(revenue_local + tax_local - freight_local - insurance_local, 2)
    fob_local = round(total_export * rate, 2)
    if abs(receivable_local - fob_local) > 1.0:
        raise VoucherError(
            "收入区金额不平：报关金额×汇率=%.2f，收入+销项税+运保费=%.2f"
            % (fob_local, receivable_local))
    add(income_summary, receivable, aux_customer=customer, currency_code=currency,
        original=total_export, local=receivable_local, debit=True)
    add(income_summary, revenue, currency_code=currency,
        original=total_revenue / rate, local=revenue_local, debit=False)
    if total_tax != 0:
        add(income_summary, "22210102", currency_code="CNY",
            original=total_tax, local=tax_local, debit=False)
    if total_freight != 0:
        add(income_summary, freight_account, currency_code=currency,
            original=-total_freight, local=freight_local, debit=True)
    if total_insurance != 0:
        add(income_summary, "560107", currency_code=currency,
            original=-total_insurance, local=insurance_local, debit=True)

    for record in records:
        product_name = to_text(record.get("报关品名"))
        inventory_code = masters.inventory_code(product_name)
        if not inventory_code:
            raise VoucherError("用友存货档案中不存在品名: " + product_name)
        category = PRODUCT_CODE_MAPPING.get(inventory_code[:4], "其他产品")
        subject = INVENTORY_SUBJECT[category]

        cost = round(to_float(record.get("不含税成本")), 2)
        if cost <= 0 and round(to_float(record.get("采购金额")), 2) <= 0:
            # 没有采购成本/金额时不能生成采购与结转分录（0 元分录无法保存）
            raise VoucherError("采购金额为0，无法生成采购分录")
        weight = to_float(record.get("报关重量"), default=0.0) or None
        unit_price = round(cost / weight, 2) if weight else None
        supplier_raw = to_text(record.get("供应商"))
        if re.search(r"[,，、;；]", supplier_raw):
            # 多值供应商不自动兼容，交由人工拆分后重跑
            raise VoucherError("供应商字段包含多个名称: " + supplier_raw)
        supplier = masters.supplier_code(record.get("供应商简称"), record.get("供应商"))
        if not supplier:
            raise VoucherError("无法匹配供应商: " + to_text(record.get("供应商简称")))

        add(contract + "采购商品", subject, aux_inventory=inventory_code,
            original=cost, local=cost, debit=True, quantity=weight, unit_price=unit_price)
        input_tax = to_float(record.get("进项税额"))
        rebate = to_float(record.get("退税额"))
        effective_tax = rebate if (input_tax == 0 and rebate != 0) else input_tax
        effective_tax = round(effective_tax, 2)
        payable = round(cost + effective_tax, 2)
        source_payable = round(to_float(record.get("采购金额")), 2)
        if abs(payable - source_payable) > 1.0:
            raise VoucherError(
                "采购区金额不平：不含税成本+税额=%.2f，采购金额=%.2f"
                % (payable, source_payable))
        if input_tax == 0 and rebate != 0:
            add(contract + "采购商品-专票", "22210103",
                original=effective_tax, local=effective_tax, debit=True)
        else:
            add(contract + "采购商品-专票", "22210101",
                original=effective_tax, local=effective_tax, debit=True)
        add(contract + "采购商品", "220201", aux_supplier=supplier,
            original=payable, local=payable, debit=False)
        add(contract + "结转采购成本", cost_account, original=cost, local=cost, debit=True)
        add(contract + "结转采购成本", subject, aux_inventory=inventory_code,
            original=cost, local=cost, debit=False, quantity=weight, unit_price=unit_price)

    return rows


def read_template_header() -> list[str]:
    book = xlrd.open_workbook(str(TEMPLATE_PATH))
    sheet = book.sheet_by_index(0)
    return [str(sheet.cell_value(0, col)) for col in range(sheet.ncols)]


def _write_row(sheet, column, row_index, row, voucher_id, posting_date):
    values = {
        "凭证ID": voucher_id,
        "会计年": posting_date.year,
        "会计期间": posting_date.month,
        "制单日期": posting_date.isoformat(),
        "凭证类别": "记",
        "制单人": voucher_maker(),
        "科目编码": row["科目编码"],
        "摘要": row["摘要"],
        "币种名称": row["币种名称"],
        "汇率": row["汇率"],
        "单价": row["单价"],
        "借方数量": row["借方数量"],
        "贷方数量": row["贷方数量"],
        "原币借方": row["原币借方"],
        "原币贷方": row["原币贷方"],
        "借方金额": row["借方金额"],
        "贷方金额": row["贷方金额"],
        "客户编码": row["客户编码"],
        "供应商编码": row["供应商编码"],
        "存货档案": row["存货档案"],
    }
    for key, value in values.items():
        if key not in column or value is None:
            continue
        sheet.write(row_index, column[key], value)


def _new_sheet(path: Path):
    header = read_template_header()
    book = xlwt.Workbook(encoding="utf-8")
    sheet = book.add_sheet("凭证")
    for index, name in enumerate(header):
        sheet.write(0, index, name)
    column = {name.lstrip("* "): idx for idx, name in enumerate(header)}
    return book, sheet, column


def write_template_xls(rows, path: Path, posting_date: datetime.date, voucher_id: int = 1):
    """把一张凭证的分录写成 53 列 .xls。"""
    book, sheet, column = _new_sheet(path)
    for row_index, row in enumerate(rows, start=1):
        _write_row(sheet, column, row_index, row, voucher_id, posting_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(str(path))


def write_multi_template(vouchers, path: Path, posting_date: datetime.date):
    """把多张凭证写进同一个 .xls，凭证ID 从 1 递增。"""
    book, sheet, column = _new_sheet(path)
    row_index = 1
    for voucher_id, rows in enumerate(vouchers, start=1):
        for row in rows:
            _write_row(sheet, column, row_index, row, voucher_id, posting_date)
            row_index += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(str(path))


def group_records(records, group_key="报关单号"):
    groups = collections.OrderedDict()
    for record in records:
        key = to_text(record.get(group_key))
        if not key:
            continue
        groups.setdefault(key, []).append(record)
    return groups


# 附加订单的写法很随意：-ADD / ADD / 空格ADD / 直接连写 / ADD1 都可能出现；
# 末尾的 -A / -B 是批次标记（如 06H056ADD1-A），属于合法写法。
# `&ADD` 是联合订单（一张报关单对应多个订单），不能拆解或继承，必须先排除。
ADD_SUFFIX = re.compile(r"^(?P<base>.+?)[-_\s]?ADD\d*(?:-[A-Z])?$", re.IGNORECASE)


def apply_add_inheritance(records) -> tuple[list[dict], list[dict]]:
    """附加订单（-ADD / ADD / 空格ADD / 连写 ADD1 等）缺客户或供应商时，继承原始订单的同名字段。

    附加订单只补空值，不覆盖已有数据；原始订单有多个供应商时不推断，保持为空。
    注意：含 `&` 的是联合订单（一张报关单对应多个订单），不属于附加订单，不参与继承。
    """
    by_contract = collections.defaultdict(list)
    for record in records:
        by_contract[to_text(record.get("合同号_1"))].append(record)

    notes = []
    for contract, group in by_contract.items():
        if "&" in contract:
            # 含 & 的是联合订单，不是附加订单
            continue
        match = ADD_SUFFIX.match(contract)
        if not match:
            continue
        base_contract = match.group("base")
        base_records = by_contract.get(base_contract)
        if not base_records and base_contract.upper().endswith("Y"):
            # Y 是佣金标记，基础订单可能不带 Y（如 07C210Y-ADD → 07C210）
            base_contract = base_contract[:-1]
            base_records = by_contract.get(base_contract)
        if not base_records:
            continue

        customers = {to_text(base.get("买方英文名")) or to_text(base.get("客户名称_脚本用"))
                     for base in base_records}
        customers.discard("")
        customer = next(iter(customers)) if len(customers) == 1 else ""
        suppliers = {to_text(r.get("供应商")) for r in base_records if to_text(r.get("供应商"))}
        short_names = {to_text(r.get("供应商简称")) for r in base_records
                       if to_text(r.get("供应商简称"))}
        single_supplier = next(iter(suppliers)) if len(suppliers) == 1 else ""
        single_short = next(iter(short_names)) if len(short_names) == 1 else ""

        for record in group:
            changed = []
            if not to_text(record.get("买方英文名")) and not to_text(record.get("客户名称_脚本用")):
                if customer:
                    record["买方英文名"] = customer
                    changed.append("客户")
            if not to_text(record.get("供应商")) and single_supplier:
                record["供应商"] = single_supplier
                record["供应商简称"] = single_short
                changed.append("供应商")
            if changed:
                notes.append({"合同号_1": contract, "报关单号": to_text(record.get("报关单号")),
                              "从": base_contract, "继承": "、".join(changed)})
    return records, notes


def generate_all(records, output_dir: Path, posting_date: datetime.date | None = None) -> dict:
    """全量生成：一个报关单号 → 一张凭证 → 一个 .xls。"""
    masters = get_masters()
    posting_date = posting_date or datetime.date.today()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    records, inherit_notes = apply_add_inheritance(list(records))
    groups = group_records(records)
    generated, skipped = [], []
    for index, (customs_no, group) in enumerate(groups.items(), start=1):
        contract = to_text(group[0].get("合同号_1"))
        try:
            rows = build_voucher(group, masters)
        except VoucherError as error:
            skipped.append({"报关单号": customs_no, "合同号_1": contract,
                            "行数": len(group), "原因": str(error)})
            continue
        safe_contract = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5&_-]", "_", contract)
        filename = "%03d_%s_%s.xls" % (index, customs_no, safe_contract)
        write_template_xls(rows, output_dir / filename, posting_date)
        generated.append({"报关单号": customs_no, "合同号_1": contract,
                          "记录数": len(group), "行数": len(rows), "文件": filename})

    return {"generated": generated, "skipped": skipped, "inherited": inherit_notes,
            "posting_date": posting_date.isoformat(), "output_dir": str(output_dir)}


# 对外（财务人员）可见的错误文案，不暴露内部异常/代码
FRIENDLY_ERRORS = [
    ("缺失字段: 供应商", "缺少供应商信息，请补全后重试"),
    ("缺失字段: 报关品名", "缺少报关品名，请补全后重试"),
    ("缺失字段: 报关金额", "缺少报关金额，请补全后重试"),
    ("缺失字段: 合同号_1", "缺少合同号，请补全后重试"),
    ("缺失字段: 销售方式", "缺少销售方式，请补全后重试"),
    ("缺失字段: 币种", "缺少币种，请补全后重试"),
    ("缺失字段: 不含税人民币收入", "缺少不含税人民币收入，请补全后重试"),
    ("缺失字段: 不含税成本", "缺少不含税成本，请补全后重试"),
    ("缺失字段: 采购金额", "缺少采购金额，请补全后重试"),
    ("无法匹配客户", "未找到对应客户档案，请确认客户名称"),
    ("无法匹配供应商", "未找到对应供应商档案，请确认供应商名称"),
    ("供应商字段包含多个名称", "供应商填写了多个名称，请拆分为单条后重试"),
    ("用友存货档案中不存在品名", "存货档案中没有该品名，请补充存货档案或核对品名后重试"),
    ("收入区金额不平", "收入、税额与报关金额不一致，请核对后重试"),
    ("采购区金额不平", "采购金额与成本、税额不一致，请核对后重试"),
    ("缺少汇率", "缺少当月汇率，请补充汇率表后重试"),
    ("采购金额为0", "没有采购金额和成本，请补全采购信息后重试"),
]


def friendly_error(message: str) -> str:
    """把内部错误转成财务人员看得懂的简短提示。"""
    text = str(message or "")
    for prefix, friendly in FRIENDLY_ERRORS:
        if text.startswith(prefix):
            if prefix == "用友存货档案中不存在品名":
                name = text.split(":", 1)[-1].strip()
                return "存货档案中没有品名「%s」，请补充或核对后重试" % name
            return friendly
    return "生成凭证时遇到问题，请检查数据后重试"


def generate_batch(records, output_path: Path, posting_date: datetime.date | None = None) -> dict:
    """多张凭证合并到一个 .xls：每个报关单号一张凭证，凭证ID 递增。"""
    masters = get_masters()
    posting_date = posting_date or datetime.date.today()
    records, inherit_notes = apply_add_inheritance(list(records))
    groups = group_records(records)

    vouchers, successes, failures = [], [], []
    for customs_no, group in groups.items():
        contract = to_text(group[0].get("合同号_1"))
        try:
            rows = build_voucher(group, masters)
        except VoucherError as error:
            reason = friendly_error(str(error))
            failures.append({"报关单号": customs_no, "合同号_1": contract,
                             "原因": reason, "记录": group})
            continue
        except Exception:  # noqa: BLE001 - 不向财务暴露内部异常
            failures.append({"报关单号": customs_no, "合同号_1": contract,
                             "原因": "生成凭证时遇到问题，请检查数据后重试", "记录": group})
            continue
        vouchers.append(rows)
        successes.append({"报关单号": customs_no, "合同号_1": contract,
                          "记录": group, "行数": len(rows)})

    if vouchers:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_multi_template(vouchers, output_path, posting_date)

    return {"file": str(output_path) if vouchers else "",
            "vouchers": len(vouchers), "successes": successes,
            "failures": failures, "inherited": inherit_notes,
            "posting_date": posting_date.isoformat()}
