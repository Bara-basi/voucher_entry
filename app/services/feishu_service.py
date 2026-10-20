"""飞书多维表只读/写回服务（凭证开具状态）。"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://open.feishu.cn/open-apis"


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


def _request(url, *, method="GET", data=None, token=None, timeout=60):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    try:
        with urlopen(Request(url, data=body, headers=headers, method=method), timeout=timeout) as response:
            return json.load(response)
    except URLError as error:
        raise FeishuError("无法连接飞书，请检查网络后重试") from error
    except Exception as error:  # noqa: BLE001 - 统一转为友好错误
        raise FeishuError("飞书返回内容无法解析，请稍后重试") from error


def tenant_token() -> str:
    values = config()
    payload = {"app_id": values["LARK_APP_ID"], "app_secret": values["LARK_APP_SECRET"]}
    body = _request(BASE + "/auth/v3/tenant_access_token/internal", method="POST", data=payload)
    if body.get("code") != 0:
        raise FeishuError("飞书鉴权失败，请检查应用配置")
    return body["tenant_access_token"]


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
        returned_ids = {r.get("record_id") for r in (body.get("data") or {}).get("records", [])}
        if returned_ids != {r["record_id"] for r in chunk}:
            raise FeishuError("飞书未确认全部记录写回，请重试")
