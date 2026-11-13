"""把交付导出从「报 ID、去下载中心」改成对话能完成的动作。

用户只提供项目名、导出方式，或一张看板上的截图。模板 ID 和任务 ID 由工具自己解析。
文件在任务中心异步生成，这里按任务编号取出 downloadFile，直接交给对话。
"""

from __future__ import annotations

import json
import time
from typing import Any

from octop.infra.auth.youli.mcn_client import McnClientError

EXPORT_DESCRIPTION = (
    "按用户说的话导出交付数据。用户不知道 ID，禁止向用户询问模板 ID、任务 ID、工具名或 confirm。"
    "从原话或截图读取项目名，以及要按项目还是按每条链接；看不清就用业务语言问，一次只问一个缺口。"
    "先用 youli_list_delivery_projects 核对名称，0 条或多项时只把名称列给用户选。"
    "按项目导出不要传 export_template_id。"
    "按链接导出时，用户没指定模板就用默认模板；只有用户说出模板名称，才用 youli_list_export_templates 对上名称后传入 id。"
    "用一句话复述项目名和导出方式，用户同意后再以 confirm=true 调用。"
    "返回 download_url 时把它作为下载链接发给用户。没有链接就用 task_id 调用 youli_get_download_task。"
    "不要让用户去下载中心或交付看板。"
)
LIST_DELIVERY_PROJECTS_DESCRIPTION = (
    "按名称核对交付看板中的项目，导出前使用。keyword 传用户说的或截图上的项目名。"
    "name_field 用 project 查娱乐/品牌名，没有结果再用 brand 查订单项目名。"
    "只把项目名称说给用户，不要展示 ID。同名记录会按这个名称一起导出。"
)
LIST_EXPORT_TEMPLATES_DESCRIPTION = (
    "查询交付看板按链接导出的模板名称。只在用户明确说出模板名称时使用。"
    "只把 templateName 说给用户，id 仅传给 youli_export_delivery。用户没指定模板时用默认模板，不要追问。"
)
GET_DOWNLOAD_TASK_DESCRIPTION = (
    "查询刚才那次交付导出是否已经生成文件。task_id 使用 youli_export_delivery 返回的任务 ID。"
    "生成后把 download_url 发给用户。不要让用户去下载中心或交付看板。"
)

_DOWNLOAD_LIST = "/api/crm/task/getTaskDownloadList"
_FILE_PREFIX = {
    "project": "交付看板按项目导出",
    "link": "交付看板按链接导出",
}
_DOWNLOAD_STATUS = {
    0: "失败",
    1: "处理中",
    2: "成功",
    3: "失败",
    4: "已撤销",
    5: "撤销中",
}
_POLL_ATTEMPTS = 4
_POLL_PAUSE_SECONDS = 1.0


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("data", "list", "records"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _slim(data: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{key: item.get(key) for key in keys if key in item} for item in _rows(data)]


def _row_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("taskId") or "").strip()


def _download_note(status_code: Any, url: str) -> str:
    if url:
        return "把 download_url 原样作为下载链接发给用户。不要让用户去下载中心或交付看板。"
    if status_code in (0, 3):
        return "导出失败。把 message 告诉用户，不要让用户去下载中心。"
    if status_code == 4:
        return "这次导出已撤销。不要让用户去下载中心。"
    return "文件还在生成。请用同一个 task_id 再调用 youli_get_download_task，不要让用户去下载中心。"


def _download_view(row: dict[str, Any] | None, task_id: str = "") -> dict[str, Any]:
    if not row:
        return {
            "task_id": task_id,
            "file_name": "",
            "status": "处理中",
            "status_code": 1,
            "download_url": "",
            "message": "",
            "note": _download_note(1, ""),
        }
    status_code = row.get("status")
    url = str(row.get("downloadFile") or "").strip()
    return {
        "task_id": _row_id(row) or task_id,
        "file_name": str(row.get("fileName") or ""),
        "status": _DOWNLOAD_STATUS.get(status_code, "处理中")
        if isinstance(status_code, int)
        else "处理中",
        "status_code": status_code if status_code is not None else 1,
        "download_url": url,
        "message": str(row.get("exceptionMessage") or ""),
        "note": _download_note(status_code, url),
    }


def _list_download_tasks(client: Any) -> list[dict[str, Any]]:
    data = client.post(_DOWNLOAD_LIST, {"page": {"pageNum": 1, "pageSize": 20}})
    return _rows(data)


def _match_download_row(
    rows: list[dict[str, Any]],
    task_id: str,
    file_prefix: str,
    known_ids: set[str] | None,
) -> dict[str, Any] | None:
    if task_id:
        for row in rows:
            if _row_id(row) == task_id:
                return row
        return None
    if known_ids is None:
        return None
    for row in rows:
        name = str(row.get("fileName") or "")
        if file_prefix and not name.startswith(file_prefix):
            continue
        if _row_id(row) in known_ids:
            continue
        return row
    return None


def _is_settled(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    status = row.get("status")
    if status in (0, 3, 4):
        return True
    return status == 2 and bool(str(row.get("downloadFile") or "").strip())


def _poll_download(
    client: Any,
    *,
    task_id: str = "",
    file_prefix: str = "",
    known_ids: set[str] | None = None,
) -> dict[str, Any]:
    found: dict[str, Any] | None = None
    for index in range(_POLL_ATTEMPTS):
        try:
            rows = _list_download_tasks(client)
        except McnClientError as exc:
            if index + 1 == _POLL_ATTEMPTS:
                view = _download_view(None, task_id)
                view["status"] = "未知"
                view["message"] = str(exc)
                view["note"] = (
                    "任务已提交，但暂时查不到文件。请稍后用 task_id 再调用 youli_get_download_task。"
                    "不要让用户去下载中心。"
                )
                return view
            time.sleep(_POLL_PAUSE_SECONDS)
            continue
        found = _match_download_row(rows, task_id, file_prefix, known_ids)
        if _is_settled(found):
            break
        if index + 1 < _POLL_ATTEMPTS:
            time.sleep(_POLL_PAUSE_SECONDS)
    return _download_view(found, task_id)


def _export_identity(result: Any) -> tuple[str, str]:
    if isinstance(result, dict):
        task_id = str(result.get("taskId") or result.get("id") or "").strip()
        file_name = str(result.get("fileName") or "")
        return task_id, file_name
    return "", ""


def list_delivery_projects(client: Any, args: dict[str, Any]) -> str:
    keyword = str(args.get("keyword") or "").strip()
    name_field = str(args.get("name_field") or "project").strip() or "project"
    payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": 10}}
    if keyword:
        if name_field == "brand":
            payload["projectBrandName"] = keyword
        elif name_field == "project":
            payload["projectName"] = keyword
        else:
            return "查询失败：name_field 只能是 project 或 brand。"
    data = client.post("/api/mcn/deliveryDashboard/project/list", payload)
    return _json(
        {
            "name_field": name_field,
            "projects": _slim(
                data,
                ("projectName", "projectBrandName", "projectTypeName", "totalOrderAmount"),
            ),
            "note": "只向用户展示项目名称。确认后按同一个名称导出，同名会一起导出。",
        }
    )


def list_export_templates(client: Any, args: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        "page": {"pageNum": 1, "pageSize": 50},
        "sceneCode": 2,
        "status": 1,
    }
    template_name = str(args.get("template_name") or "").strip()
    if template_name:
        payload["templateName"] = template_name
    data = client.post("/api/mcn/excel/export/template/list", payload)
    return _json(
        {
            "templates": _slim(data, ("id", "templateName", "isDefault")),
            "note": "只把 templateName 说给用户。用户没指定模板时，导出不要传模板 id。",
        }
    )


def get_download_task(client: Any, args: dict[str, Any]) -> str:
    task_id = str(args.get("task_id") or "").strip()
    file_prefix = str(args.get("file_prefix") or "").strip()
    if not task_id and not file_prefix:
        return "未查询：请传入 youli_export_delivery 返回的 task_id。"
    # 跨轮次只凭文件名前缀无法区分新旧任务，不能返回历史文件。
    if not task_id:
        return _json(
            {
                "status": "未知",
                "download_url": "",
                "file_prefix": file_prefix,
                "note": "缺少本次任务编号，无法安全关联下载文件，请核对本次导出任务。",
            }
        )
    known_ids: set[str] | None = None
    view = _poll_download(
        client,
        task_id=task_id,
        file_prefix=file_prefix,
        known_ids=known_ids,
    )
    if not task_id:
        view["file_prefix"] = file_prefix
    return _json(view)


def export_delivery_result(client: Any, args: dict[str, Any]) -> str:
    if not bool(args.get("confirm")):
        return (
            "未导出：先用业务语言向用户复述项目名称和导出方式（按项目或按链接）。"
            "用户同意后再以 confirm=true 调用。不要向用户询问 ID。"
        )
    mode = str(args.get("mode") or "project").strip()
    if mode not in _FILE_PREFIX:
        return "导出失败：导出方式只能是按项目或按链接。"
    payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": 20}}
    project_name = str(args.get("project_name") or "").strip()
    if project_name:
        payload["projectName"] = project_name
    opportunity_id = str(args.get("customer_opportunity_id") or "").strip()
    if opportunity_id:
        payload["customerOpportunityIds"] = [opportunity_id]
    template_id = str(args.get("export_template_id") or "").strip()
    # 按项目导出没有模板选择，页面也不传这个字段。
    if mode == "link" and template_id:
        payload["exportTemplateId"] = template_id
    file_prefix = _FILE_PREFIX[mode]
    try:
        known_ids = {_row_id(row) for row in _list_download_tasks(client)}
    except McnClientError:
        known_ids = None
    path = (
        "/api/mcn/deliveryDashboard/project/export"
        if mode == "project"
        else "/api/mcn/deliveryDashboard/link/export"
    )
    result = client.post(path, payload)
    if result is False or result is None:
        return "导出失败：任务没有创建。"
    task_id, file_name = _export_identity(result)
    view = _poll_download(
        client,
        task_id=task_id,
        file_prefix=file_prefix,
        known_ids=None if task_id else known_ids,
    )
    if task_id and not view.get("task_id"):
        view["task_id"] = task_id
    if file_name and not view.get("file_name"):
        view["file_name"] = file_name
    view["mode"] = mode
    view["file_prefix"] = file_prefix
    if project_name:
        view["project_name"] = project_name
    return _json(view)
