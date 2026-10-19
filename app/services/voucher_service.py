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

ROOT = Path(__file__).resolve().parents[2]

TEMPLATE_PATH = ROOT / "templates/024_存货档案_反方向数量留空.xls"
INVENTORY_PATH = ROOT / "mappings/platform/存货档案.json"
CUSTOMER_PATH = ROOT / "mappings/platform/客户档案.json"
SUPPLIER_PATH = ROOT / "mappings/platform/供应商档案.json"
CUSTOMER_ALIAS_PATH = ROOT / "mappings/customer_alias_mappings.json"
CUSTOMER_HISTORY_PATH = ROOT / "mappings/customer_resolution_history.json"
SUPPLIER_ALIAS_PATH = ROOT / "mappings/finance_supplier_alias_mappings.json"
RATE_PATH = ROOT / "data/fx_rates_ai_snapshot.json"

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
            parts.append(item.get("text") if isinstance(item, dict) else item)
        return " ".join(str(p) for p in parts if p).strip()
    return str(value or "").strip()


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class MasterData:
    """客户 / 供应商 / 存货 / 汇率档案。"""

    def __init__(self):
        self.inventory = self._load_inventory()
        self.customers = read_json(CUSTOMER_PATH)["records"]
        self.suppliers = read_json(SUPPLIER_PATH)["records"]
        self.customer_aliases = {normalize_name(a["alias"]): a["platform_code"]
                                 for a in read_json(CUSTOMER_ALIAS_PATH)["aliases"]}
        self.customer_history = {}
        for record in read_json(CUSTOMER_HISTORY_PATH)["records"]:
            self.customer_history.setdefault(record["normalized_alias"], record["platform_code"])
        self.supplier_aliases = {m["alias"]: m["platform_code"]
                                 for m in read_json(SUPPLIER_ALIAS_PATH)["mappings"]}
        self.supplier_by_name = {normalize_name(s["name"]): s["code"] for s in self.suppliers}
        self.customer_by_name = collections.defaultdict(set)
        for customer in self.customers:
            self.customer_by_name[normalize_name(customer["name"])].add(customer["code"])
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

    def customer_code(self, names) -> str | None:
        for raw in names:
            name = to_text(raw)
            if not name:
                continue
            normalized = normalize_name(name)
            code = self.customer_aliases.get(normalized) or self.customer_history.get(normalized)
            if code:
                return code
            candidates = set(self.customer_by_name.get(normalized, set()))
            if not candidates:
                # 档案全名常带括号补充说明（如 TOROCA(SECIME...)），允许唯一前缀匹配
                key = normalized.replace("and", "")
                if len(key) >= 6:
                    for archive_key, codes in self.customer_by_name.items():
                        other = archive_key.replace("and", "")
                        if other.startswith(key) or key.startswith(other):
                            candidates |= codes
            if len(candidates) == 1:
                return next(iter(candidates))
            if len(candidates) > 1:
                # 同名多条时优先用正式的数字编码（如 020155 而非 2-010）
                numeric = [c for c in candidates if str(c).isdigit()]
                if len(numeric) == 1:
                    return numeric[0]
        return None

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

    customer = masters.customer_code([records[0].get("买方英文名"),
                                      records[0].get("客户名称_脚本用")])
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
    add(income_summary, receivable, aux_customer=customer, currency_code=currency,
        original=total_export, local=total_export * rate, debit=True)
    add(income_summary, revenue, currency_code=currency,
        original=total_revenue / rate, local=total_revenue, debit=False)
    if total_tax != 0:
        add(income_summary, "22210102", currency_code="CNY",
            original=total_tax, local=total_tax, debit=False)
    if total_freight != 0:
        add(income_summary, freight_account, currency_code=currency,
            original=-total_freight, local=-total_freight * rate, debit=True)
    if total_insurance != 0:
        add(income_summary, "560107", currency_code=currency,
            original=-total_insurance, local=-total_insurance * rate, debit=True)

    for record in records:
        product_name = to_text(record.get("报关品名"))
        inventory_code = masters.inventory_code(product_name)
        if not inventory_code:
            raise VoucherError("用友存货档案中不存在品名: " + product_name)
        category = PRODUCT_CODE_MAPPING.get(inventory_code[:4], "其他产品")
        subject = INVENTORY_SUBJECT[category]

        cost = to_float(record.get("不含税成本"))
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
        if input_tax == 0 and rebate != 0:
            add(contract + "采购商品-专票", "22210103",
                original=rebate, local=rebate, debit=True)
        else:
            add(contract + "采购商品-专票", "22210101",
                original=input_tax, local=input_tax, debit=True)
        add(contract + "采购商品", "220201", aux_supplier=supplier,
            original=to_float(record.get("采购金额")), local=to_float(record.get("采购金额")),
            debit=False)
        add(contract + "结转采购成本", cost_account, original=cost, local=cost, debit=True)
        add(contract + "结转采购成本", subject, aux_inventory=inventory_code,
            original=cost, local=cost, debit=False, quantity=weight, unit_price=unit_price)

    return rows


def read_template_header() -> list[str]:
    book = xlrd.open_workbook(str(TEMPLATE_PATH))
    sheet = book.sheet_by_index(0)
    return [str(sheet.cell_value(0, col)) for col in range(sheet.ncols)]


def write_template_xls(rows, path: Path, posting_date: datetime.date):
    """把一张凭证的分录写成 53 列 .xls。"""
    header = read_template_header()
    book = xlwt.Workbook(encoding="utf-8")
    sheet = book.add_sheet("凭证")
    for index, name in enumerate(header):
        sheet.write(0, index, name)
    column = {name.lstrip("* "): idx for idx, name in enumerate(header)}

    for row_index, row in enumerate(rows, start=1):
        values = {
            "凭证ID": 1,
            "会计年": posting_date.year,
            "会计期间": posting_date.month,
            "制单日期": posting_date.isoformat(),
            "凭证类别": "记",
            "制单人": "王益",
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


def generate_all(records, output_dir: Path, posting_date: datetime.date | None = None) -> dict:
    """全量生成：一个报关单号 → 一张凭证 → 一个 .xls。"""
    masters = get_masters()
    posting_date = posting_date or datetime.date.today()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

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

    return {"generated": generated, "skipped": skipped,
            "posting_date": posting_date.isoformat(), "output_dir": str(output_dir)}
