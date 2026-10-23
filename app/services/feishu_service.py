"""飞书多维表只读/写回服务（凭证开具状态）。"""
from __future__ import annotations

import json
import http.client
import socket
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://open.feishu.cn/open-apis"
_THREAD_LOCAL = threading.local()
_TOKEN_LOCK = threading.Lock()
_TOKEN = {"value": "", "expires_at": 0.0}


class FeishuError(Exception):
    """飞书接口调用失败（对外只暴露友好文案）。"""


def config() -> dict:
    values = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _connection():
    connection = getattr(_THREAD_LOCAL, "connection", None)
    if connection is None:
        connection = http.client.HTTPSConnection(
            "open.feishu.cn", 443, timeout=60,
            context=ssl.create_default_context(),
        )
        _THREAD_LOCAL.connection = connection
    return connection


def _close_connection():
    connection = getattr(_THREAD_LOCAL, "connection", None)
    if connection is not None:
        try:
            connection.close()
        finally:
            _THREAD_LOCAL.connection = None


def _request(url, *, method="GET", data=None, token=None, timeout=60):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    parsed = urlsplit(url)
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    for attempt in range(2):
        try:
            connection = _connection()
            connection.timeout = timeout
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            if response.will_close:
                _close_connection()
            return json.loads(raw.decode("utf-8"))
        except PermissionError as error:
            _close_connection()
            if getattr(error, "winerror", None) == 10013:
                raise FeishuError(
                    "工作台进程没有网络访问权限（WinError 10013），"
                    "请从普通终端或服务管理器重新启动后台服务"
                ) from error
            raise FeishuError("无法连接飞书，请检查网络、DNS 或代理后重试") from error
        except (OSError, http.client.HTTPException, socket.gaierror, ssl.SSLError) as error:
            _close_connection()
            if attempt == 0:
                continue
            raise FeishuError("无法连接飞书，请检查网络、DNS 或代理后重试") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            _close_connection()
            raise FeishuError("飞书返回内容无法解析，请稍后重试") from error
    raise FeishuError("无法连接飞书，请检查网络、DNS 或代理后重试")


def tenant_token() -> str:
    with _TOKEN_LOCK:
        if _TOKEN["value"] and _TOKEN["expires_at"] > time.time() + 60:
            return _TOKEN["value"]
    values = config()
    payload = {"app_id": values["LARK_APP_ID"], "app_secret": values["LARK_APP_SECRET"]}
    body = _request(BASE + "/auth/v3/tenant_access_token/internal", method="POST", data=payload)
    if body.get("code") != 0:
        raise FeishuError("飞书鉴权失败，请检查应用配置")
    token = body["tenant_access_token"]
    expire_seconds = int(body.get("expire") or 7200)
    with _TOKEN_LOCK:
        _TOKEN.update(value=token, expires_at=time.time() + expire_seconds)
    return token


def table_target() -> tuple[str, str]:
    """当前凭证主数据表：海关AI / 2026年报关数据。"""
    values = config()
    if values.get("MT_BAO_GUAN_APP_TOKEN") and values.get("MT_BAO_GUAN_TABLE_ID"):
        return values["MT_BAO_GUAN_APP_TOKEN"], values["MT_BAO_GUAN_TABLE_ID"]
    return values["MT_FINANCE_AI_APP_TOKEN"], values["MT_FINANCE_AI_TABLE_ID"]


def list_records() -> list[dict]:
    """分页读取整张表，返回 [{'record_id':..., 字段...}]。"""
    token = tenant_token()
    app_token, table_id = table_target()
    url = f"{BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    records, cursor = [], None
    while True:
        params = {"page_size": 500}
        if cursor:
            params["page_token"] = cursor
        body = _request(url + "?" + urlencode(params), token=token)
        if body.get("code") != 0:
            raise FeishuError("读取飞书记录失败，请稍后重试")
        data = body.get("data") or {}
        for item in data.get("items") or []:
            record = dict(item.get("fields") or {})
            record["record_id"] = item.get("record_id")
            records.append(record)
        if not data.get("has_more"):
            break
        cursor = data.get("page_token")
    return records


def batch_update(updates: list[dict]) -> None:
    """批量写回字段。updates: [{'record_id': ..., 'fields': {...}}]，每批最多 100 条。"""
    if not updates:
        return
    token = tenant_token()
    app_token, table_id = table_target()
    url = f"{BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
    for start in range(0, len(updates), 100):
        chunk = updates[start:start + 100]
        body = _request(url, method="POST", data={"records": chunk}, token=token)
        if body.get("code") != 0:
            raise FeishuError("写回飞书失败，请稍后重试")
        returned = (body.get("data") or {}).get("records", [])
        returned_ids = {r.get("record_id") for r in returned}
        if returned_ids != {r["record_id"] for r in chunk}:
            raise FeishuError("飞书未确认全部记录写回，请重试")
        by_id = {r.get("record_id"): r.get("fields") or {} for r in returned}
        for item in chunk:
            actual_fields = by_id.get(item["record_id"], {})
            for field, expected in item["fields"].items():
                actual = actual_fields.get(field)
                if str(actual).strip() != str(expected).strip():
                    raise FeishuError(
                        "飞书未确认字段“" + field + "”更新，请刷新后重试"
                    )
