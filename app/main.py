"""凭证构建工作台后端（FastAPI）。"""
from __future__ import annotations

import datetime
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.services import voucher_service as svc
from app.services import feishu_service as feishu

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "data/ai_customs_source.json"
DEFAULT_OUTPUT = ROOT / "outputs/single_voucher"
BATCH_OUTPUT = ROOT / "outputs/batch"

PENDING_FIELD = "是否开凭证"
VOUCHER_DATE_FIELD = "凭证开具日期"
VOUCHER_RESULT_FIELD = "凭证开具结果"

from app.workbench_api import router as workbench_router
from app.services.workbench_service import resume as resume_workbench


@asynccontextmanager
async def lifespan(app: FastAPI):
    resume_workbench()
    yield


app = FastAPI(title="凭证构建工作台", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def cache_headers(request, call_next):
    """Always revalidate the HTML entry point; hashed assets may be cached for long."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/"):
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif path.startswith("/api/") or path == "/health":
        response.headers.setdefault("Cache-Control", "no-store")
    else:
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


app.include_router(workbench_router)


class GenerateRequest(BaseModel):
    """生成参数。"""
    posting_date: str | None = None
    contract_prefix: str = "26MT"
    sale_method: str = "出口"
    output_dir: str | None = None


class PendingRequest(BaseModel):
    """待开凭证一键生成。"""
    mark: bool = True
    posting_date: str | None = None


class BulkRequest(BaseModel):
    """按出口日期范围批量生成。"""
    start_date: str
    end_date: str
    mark: bool = False
    posting_date: str | None = None


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


def _within_scope(record) -> bool:
    contract = str(record.get("合同号_1") or "").upper()
    return contract.startswith("26MT") and str(record.get("销售方式")) == "出口"


def _export_ts(record):
    value = record.get("出口日期")
    return value if isinstance(value, (int, float)) else None


@app.get("/vouchers/pending")
def pending_list():
    """列出飞书中凭证状态为「待开」的记录。"""
    records = [r for r in feishu.list_records()
               if _within_scope(r) and str(r.get(PENDING_FIELD)) == "待开"]
    return {"total": len(records),
            "contracts": sorted({str(r.get("合同号_1")) for r in records})}


@app.post("/vouchers/generate-pending")
def generate_pending(request: PendingRequest):
    """读取「待开」记录 → 合并生成一个导入模板 → 写回飞书状态。"""
    records = [r for r in feishu.list_records()
               if _within_scope(r) and str(r.get(PENDING_FIELD)) == "待开"]
    if not records:
        raise HTTPException(status_code=422, detail="当前没有状态为「待开」的凭证记录")

    posting_date = (datetime.date.fromisoformat(request.posting_date)
                    if request.posting_date else datetime.date.today())
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output = BATCH_OUTPUT / ("待开凭证_%s.xls" % stamp)
    result = svc.generate_batch(records, output, posting_date)

    written = 0
    if request.mark:
        month = posting_date.strftime("%Y-%m")
        updates = []
        for success in result["successes"]:
            for record in success["记录"]:
                updates.append({"record_id": record.get("record_id"),
                                "fields": {PENDING_FIELD: "已开",
                                           VOUCHER_DATE_FIELD: month,
                                           VOUCHER_RESULT_FIELD: "保存成功"}})
        for failure in result["failures"]:
            for record in failure["记录"]:
                updates.append({"record_id": record.get("record_id"),
                                "fields": {VOUCHER_RESULT_FIELD: failure["原因"]}})
        feishu.batch_update(updates)
        written = len(updates)

    return {
        "file": result["file"],
        "vouchers": result["vouchers"],
        "records": len(records),
        "written": written,
        "failures": [{"合同号_1": f["合同号_1"], "报关单号": f["报关单号"],
                      "原因": f["原因"]} for f in result["failures"]],
        "inherited": result["inherited"],
    }


@app.post("/vouchers/generate-bulk")
def generate_bulk(request: BulkRequest):
    """按出口日期范围批量生成（默认不写回，mark=true 时回写状态）。"""
    try:
        start = datetime.date.fromisoformat(request.start_date)
        end = datetime.date.fromisoformat(request.end_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="日期格式应为 YYYY-MM-DD") from None
    if start > end:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")

    start_ts = datetime.datetime.combine(start, datetime.time.min).timestamp() * 1000
    end_ts = datetime.datetime.combine(end, datetime.time.max).timestamp() * 1000
    records = []
    for record in feishu.list_records():
        if not _within_scope(record):
            continue
        if str(record.get(PENDING_FIELD)) == "已开":
            continue
        export_ts = _export_ts(record)
        if export_ts is None or not (start_ts <= export_ts <= end_ts):
            continue
        records.append(record)
    if not records:
        raise HTTPException(status_code=422, detail="该日期范围内没有待开具凭证的记录")

    posting_date = (datetime.date.fromisoformat(request.posting_date)
                    if request.posting_date else datetime.date.today())
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output = BATCH_OUTPUT / ("批量凭证_%s_%s_%s.xls"
                             % (request.start_date, request.end_date, stamp))
    result = svc.generate_batch(records, output, posting_date)

    written = 0
    if request.mark:
        month = posting_date.strftime("%Y-%m")
        updates = []
        for success in result["successes"]:
            for record in success["记录"]:
                updates.append({"record_id": record.get("record_id"),
                                "fields": {PENDING_FIELD: "已开",
                                           VOUCHER_DATE_FIELD: month,
                                           VOUCHER_RESULT_FIELD: "保存成功"}})
        for failure in result["failures"]:
            for record in failure["记录"]:
                updates.append({"record_id": record.get("record_id"),
                                "fields": {VOUCHER_RESULT_FIELD: failure["原因"]}})
        feishu.batch_update(updates)
        written = len(updates)

    return {
        "file": result["file"],
        "vouchers": result["vouchers"],
        "records": len(records),
        "written": written,
        "failures": [{"合同号_1": f["合同号_1"], "报关单号": f["报关单号"],
                      "原因": f["原因"]} for f in result["failures"]],
    }


# Mount only built assets. Business data, credentials and mappings are never served.
FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend/dist"
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="workbench")
