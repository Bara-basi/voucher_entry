"""凭证构建工作台后端（FastAPI）。"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.services import voucher_service as svc

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "data/ai_customs_source.json"
DEFAULT_OUTPUT = ROOT / "outputs/single_voucher"

app = FastAPI(title="凭证构建工作台", version="0.1.0")


class GenerateRequest(BaseModel):
    """生成参数。"""
    posting_date: str | None = None
    contract_prefix: str = "26MT"
    sale_method: str = "出口"
    output_dir: str | None = None


def load_source() -> list[dict]:
    if not SOURCE_PATH.exists():
        raise HTTPException(status_code=503, detail="缺少飞书数据快照，请先运行 scripts/read_feishu.py")
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    return [dict(record["fields"], record_id=record["record_id"])
            for record in payload["records"]]


def in_scope(record, prefix: str, sale_method: str) -> bool:
    contract = str(record.get("合同号_1") or "").upper()
    return contract.startswith(prefix.upper()) and str(record.get("销售方式")) == sale_method


@app.get("/health")
def health():
    return {"status": "ok", "source": SOURCE_PATH.name,
            "source_exists": SOURCE_PATH.exists()}


@app.get("/vouchers/groups")
def list_groups(prefix: str = "26MT", sale_method: str = "出口"):
    """列出待生成的凭证明细分组（一个报关单号一组）。"""
    records = [r for r in load_source() if in_scope(r, prefix, sale_method)]
    groups = svc.group_records(records)
    return {"total": len(groups),
            "groups": [{"报关单号": key, "合同号_1": group[0].get("合同号_1"),
                        "记录数": len(group)} for key, group in groups.items()]}


@app.get("/vouchers/preview")
def preview(customs_no: str):
    """预览单个报关单号生成的凭证分录（不落盘）。"""
    records = [r for r in load_source() if str(r.get("报关单号") or "").strip() == customs_no]
    if not records:
        raise HTTPException(status_code=404, detail="未找到该报关单号的记录")
    try:
        rows = svc.build_voucher(records)
    except svc.VoucherError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return {"报关单号": customs_no, "合同号_1": records[0].get("合同号_1"), "rows": rows}


@app.post("/vouchers/generate")
def generate(request: GenerateRequest):
    """全量生成：一个报关单号 → 一张凭证 → 一个 .xls。"""
    records = [r for r in load_source()
               if in_scope(r, request.contract_prefix, request.sale_method)]
    if not records:
        raise HTTPException(status_code=422, detail="范围内没有记录")
    posting_date = (datetime.date.fromisoformat(request.posting_date)
                    if request.posting_date else datetime.date.today())
    output_dir = Path(request.output_dir) if request.output_dir else DEFAULT_OUTPUT
    result = svc.generate_all(records, output_dir, posting_date)
    return {
        "posting_date": result["posting_date"],
        "output_dir": result["output_dir"],
        "generated": len(result["generated"]),
        "skipped": len(result["skipped"]),
        "skipped_detail": result["skipped"],
    }
