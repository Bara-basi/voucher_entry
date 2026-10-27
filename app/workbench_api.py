"""HTTP boundary for the shared, unauthenticated intranet workbench."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Header, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .services import workbench_service as svc
from .services import workbench_store as store
from .services import draft_service

router = APIRouter(prefix="/api")


class TaskRequest(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    request_id: str


class EditRequest(BaseModel):
    version: int
    manual_exception: bool | None = None
    header: dict[str, str]
    rows: list[dict[str, Any]]
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


@router.get("/meta")
def meta():
    with store.connect() as db:
        version = svc.mapping_state(db)["version"]
        source = store.get(db, "settings", "source") or {"label": svc.initial_source_label()}
    return {"source": source["label"], "scope": "外销 · 内销",
            "offline": svc.OFFLINE, "writeback_enabled": not svc.OFFLINE,
            "mapping_version": version, "timezone": "Asia/Shanghai",
            "today": store.now()[:10], "source_fields": draft_service.SOURCE_FIELDS}


@router.post("/tasks")
def create_task(request: TaskRequest, x_session_id: str = Header(default="anonymous")):
    return svc.create_task(request.kind, request.payload, x_session_id, request.request_id)


@router.get("/tasks")
def tasks():
    with store.connect() as db:
        visible = [t for t in store.recent_items(db, "task", 100)
                   if not (t["kind"] == "query"
                           and t["payload"].get("mode", "pending") != "pending")]
        return [svc.task_view(t) for t in visible]


@router.get("/tasks/{key}")
def task(key: str):
    with store.connect() as db:
        return svc.task_view(svc.required(db, "task", key))


@router.post("/tasks/{key}/retry")
def retry(key: str):
    with store.connect(True) as db:
        value = svc.required(db, "task", key)
        if value["kind"] not in ("query", "build", "export", "writeback"):
            svc.fail(422, "未知任务")
        svc.validate_task_payload(value["kind"], value["payload"])
        if value["status"] == "done":
            return svc.task_view(value)
        value["status"] = "queued"
        store.put(db, "task", key, value)
    svc.submit(key)
    return svc.task_view(value)


@router.get("/snapshots/{key}")
def snapshot(key: str):
    with store.connect() as db:
        value = svc.required(db, "snapshot", key)
    value.pop("all_records", None)
    return value


@router.get("/batches")
def batches(workbench: bool = False):
    with store.connect() as db:
        return svc.workbench_batches(db) if workbench else store.all_items(db, "batch")


@router.get("/batches/{key}")
def batch(key: str, workbench: bool = False):
    with store.connect(True) as db:
        value = svc.required(db, "batch", key)
        if workbench:
            value = next((b for b in svc.workbench_batches(db) if b["id"] == key),
                         {**value, "draft_ids": []})
        value["drafts"] = [svc.refresh_generated_draft(db, d) for d in value["draft_ids"]]
        version = svc.mapping_state(db)["version"]
    catalogs = svc.catalog()
    for d in value["drafts"]:
        draft_service.validate(d, catalogs)
        d["mapping_stale"] = d["mapping_version"] != version
    return value


@router.get("/mapping-recalculation")
def mapping_recalculation_plan():
    return svc.mapping_recalculation_plan()


@router.post("/mapping-recalculation")
def mapping_recalculation(request: dict, x_session_id: str = Header(default="anonymous")):
    return svc.recalculate_mappings(request, x_session_id)


@router.get("/drafts/{key}")
def draft(key: str):
    with store.connect(True) as db:
        value = svc.refresh_generated_draft(db, key)
        draft_service.validate(value, svc.catalog())
        value["mapping_stale"] = value["mapping_version"] != svc.mapping_state(db)["version"]
    return value


@router.patch("/drafts/{key}")
def edit(key: str, request: EditRequest, x_session_id: str = Header(default="anonymous")):
    return svc.save_draft(key, request.model_dump(), x_session_id)


@router.post("/drafts/{key}/recalculate")
def recalculate(key: str, request: dict, x_session_id: str = Header(default="anonymous")):
    return svc.recalculate(key, request, x_session_id)


@router.get("/catalogs")
def catalogs():
    data = svc.catalog()
    return {k: v for k, v in data.items() if not k.endswith("_by_code")}


@router.get("/mappings")
def mappings():
    with store.connect() as db:
        return svc.mapping_state(db)


@router.get("/catalog-edits")
def catalog_edits():
    with store.connect() as db:
        state = store.get(db, "settings", "catalog_edits") or {"version": 1}
    return {"version": state["version"]}


@router.put("/catalogs/{kind}")
def save_catalog(kind: str, request: dict, x_session_id: str = Header(default="anonymous")):
    return svc.save_catalog(kind, request, x_session_id)


@router.put("/mappings/{kind}")
def save_mapping(kind: str, request: dict, x_session_id: str = Header(default="anonymous")):
    return svc.save_mapping(kind, request, x_session_id)


@router.get("/history/{entity}")
def history(entity: str, limit: int = Query(default=100, ge=1, le=500)):
    with store.connect() as db:
        return store.audit_items(db, entity, limit)


@router.delete("/batches/{key}")
def delete_batch(key: str, x_session_id: str = Header(default="anonymous")):
    return svc.delete_batch(key, x_session_id)


@router.post("/exports/preview")
def export_preview(request: dict):
    return svc.export_plan(request.get("draft_ids", []))


@router.get("/exports")
def exports():
    with store.connect() as db:
        entries = store.all_items(db, "export")
    return [{k: v for k, v in e.items() if k not in ("drafts", "updates", "record_ids")} |
            {"vouchers": len(e["drafts"]), "records": len(e["record_ids"]),
             "contracts": [c for d in e["drafts"] for c in d["contracts"]]} for e in entries]


@router.get("/exports/{key}")
def export_detail(key: str):
    with store.connect() as db:
        entry = svc.required(db, "export", key)
    return {k: v for k, v in entry.items() if k != "updates"}


@router.get("/exports/{key}/download")
def download(key: str):
    with store.connect() as db:
        entry = svc.required(db, "export", key)
    path = svc.OUTPUT / entry["filename"]
    if not path.is_file():
        svc.fail(404, "导出文件不存在，请联系维护人员恢复文件")
    return FileResponse(path, filename=entry["filename"], media_type="application/vnd.ms-excel")
