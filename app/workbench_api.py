"""HTTP boundary for the shared, unauthenticated intranet workbench."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Header
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
    header: dict[str, str]
    rows: list[dict[str, str]]
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


@router.get("/meta")
def meta():
    with store.connect() as db:
        version = svc.mapping_state(db)["version"]
        source = store.get(db, "settings", "source") or {"label": svc.initial_source_label()}
    return {"source": source["label"], "scope": "26MT · 出口",
            "offline": svc.OFFLINE, "mapping_version": version, "timezone": "Asia/Shanghai",
            "today": store.now()[:10], "source_fields": draft_service.SOURCE_FIELDS}


@router.post("/tasks")
def create_task(request: TaskRequest, x_session_id: str = Header(default="anonymous")):
    return svc.create_task(request.kind, request.payload, x_session_id, request.request_id)


@router.get("/tasks")
def tasks():
    with store.connect() as db:
        return [svc.task_view(t) for t in store.all_items(db, "task")][:100]


@router.get("/tasks/{key}")
def task(key: str):
    with store.connect() as db:
        return svc.task_view(svc.required(db, "task", key))


@router.post("/tasks/{key}/retry")
def retry(key: str):
    with store.connect(True) as db:
        value = svc.required(db, "task", key)
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
def batches():
    with store.connect() as db:
        return store.all_items(db, "batch")


@router.get("/batches/{key}")
def batch(key: str):
    with store.connect() as db:
        value = svc.required(db, "batch", key)
        value["drafts"] = [svc.required(db, "draft", d) for d in value["draft_ids"]]
        version = svc.mapping_state(db)["version"]
    for d in value["drafts"]:
        d["mapping_stale"] = d["mapping_version"] != version
    return value


@router.get("/drafts/{key}")
def draft(key: str):
    with store.connect() as db:
        value = svc.required(db, "draft", key)
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


@router.put("/mappings/{kind}")
def save_mapping(kind: str, request: dict, x_session_id: str = Header(default="anonymous")):
    return svc.save_mapping(kind, request, x_session_id)


@router.get("/history/{entity}")
def history(entity: str):
    with store.connect() as db:
        return [a for a in store.all_items(db, "audit") if a["entity"] == entity]


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
