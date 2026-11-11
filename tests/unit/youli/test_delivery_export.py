"""交付导出：按名称核对，并把任务中心的文件链接交回对话。"""

from __future__ import annotations

import json

from octop.infra.auth.youli import delivery_export
from octop.infra.connectors.gateway.adapters import mcn


class _Client:
    def __init__(self, rows_by_call: list[object], export_result: object) -> None:
        self.rows_by_call = list(rows_by_call)
        self.export_result = export_result
        self.calls: list[tuple[str, object]] = []

    def post(self, path: str, payload: object = None) -> object:
        self.calls.append((path, payload))
        if path == delivery_export._DOWNLOAD_LIST:
            if self.rows_by_call:
                return self.rows_by_call.pop(0)
            return []
        return self.export_result


def _ready(task_id: str = "99") -> dict[str, object]:
    return {
        "id": task_id,
        "fileName": "交付看板按项目导出251111120000",
        "status": 2,
        "downloadFile": "https://files.example/dufu.xlsx",
        "exceptionMessage": "",
    }


def test_export_returns_download_url_for_the_new_task(monkeypatch) -> None:
    monkeypatch.setattr(delivery_export.time, "sleep", lambda _seconds: None)
    old = {
        "id": "1",
        "fileName": "交付看板按项目导出251110120000",
        "status": 2,
        "downloadFile": "https://files.example/old.xlsx",
    }
    client = _Client(
        rows_by_call=[[old], [_ready(), old]],
        export_result={"taskId": "99", "fileName": "交付看板按项目导出251111120000"},
    )

    result = json.loads(
        delivery_export.export_delivery_result(
            client,
            {"mode": "project", "project_name": "杜甫江阁", "confirm": True},
        )
    )

    assert result["task_id"] == "99"
    assert result["download_url"] == "https://files.example/dufu.xlsx"
    assert result["project_name"] == "杜甫江阁"
    assert "不要" in result["note"]
    export_payload = [
        payload
        for path, payload in client.calls
        if path.endswith("/project/export")
    ][0]
    assert export_payload["projectName"] == "杜甫江阁"
    assert "exportTemplateId" not in export_payload


def test_export_does_not_attach_an_older_file(monkeypatch) -> None:
    monkeypatch.setattr(delivery_export.time, "sleep", lambda _seconds: None)
    old = _ready("1")
    old["downloadFile"] = "https://files.example/old.xlsx"
    client = _Client(
        rows_by_call=[[old], [old], [old], [old], [old]],
        export_result=True,
    )

    result = json.loads(
        delivery_export.export_delivery_result(
            client, {"mode": "project", "project_name": "杜甫江阁", "confirm": True}
        )
    )

    assert result["download_url"] == ""
    assert result["task_id"] == ""


def test_followup_without_task_id_does_not_return_a_historical_file() -> None:
    client = _Client(rows_by_call=[[_ready("old")]], export_result=True)
    result = json.loads(delivery_export.get_download_task(
        client, {"file_prefix": "交付看板按项目导出"}))
    assert result["download_url"] == ""
    assert client.calls == []


def test_lookup_tools_use_names_not_ids() -> None:
    calls: list[tuple[str, object]] = []

    class Client:
        def post(self, path: str, payload: object = None) -> object:
            calls.append((path, payload))
            if path.endswith("/template/list"):
                return [
                    {
                        "id": "12",
                        "templateName": "客户周报",
                        "isDefault": 0,
                        "fields": [{}],
                    }
                ]
            return [
                {
                    "subProjectId": "88",
                    "projectName": "杜甫江阁",
                    "projectBrandName": "杜甫江阁投放",
                    "projectTypeName": "冲榜",
                    "totalOrderAmount": 100,
                }
            ]

    client = Client()
    projects = json.loads(
        delivery_export.list_delivery_projects(
            client, {"keyword": "杜甫江阁", "name_field": "project"}
        )
    )
    templates = json.loads(
        delivery_export.list_export_templates(client, {"template_name": "周报"})
    )

    assert projects["projects"] == [
        {
            "projectName": "杜甫江阁",
            "projectBrandName": "杜甫江阁投放",
            "projectTypeName": "冲榜",
            "totalOrderAmount": 100,
        }
    ]
    assert "subProjectId" not in projects["projects"][0]
    assert calls[0] == (
        "/api/mcn/deliveryDashboard/project/list",
        {
            "page": {"pageNum": 1, "pageSize": 10},
            "projectName": "杜甫江阁",
        },
    )
    assert templates["templates"] == [
        {"id": "12", "templateName": "客户周报", "isDefault": 0}
    ]
    assert calls[1][0] == "/api/mcn/excel/export/template/list"
    assert calls[1][1]["sceneCode"] == 2
    assert calls[1][1]["templateName"] == "周报"


def test_connector_export_returns_link(monkeypatch) -> None:
    monkeypatch.setattr(delivery_export.time, "sleep", lambda _seconds: None)
    client = _Client(
        rows_by_call=[[], [_ready("99")]],
        export_result={"taskId": "99", "fileName": "交付看板按项目导出251111120000"},
    )
    monkeypatch.setattr(mcn, "_client", lambda creds: client)

    result = json.loads(
        mcn.call_tool(
            {"token": "t"},
            "youli_export_delivery",
            {"mode": "project", "project_name": "杜甫江阁", "confirm": True},
        )
    )

    assert result["download_url"] == "https://files.example/dufu.xlsx"
    names = [tool["name"] for tool in mcn.list_tools()]
    assert "youli_list_delivery_projects" in names
    assert "youli_list_export_templates" in names
    assert "youli_get_download_task" in names


def test_link_export_keeps_template_and_followup_uses_task_id(monkeypatch) -> None:
    monkeypatch.setattr(delivery_export.time, "sleep", lambda _seconds: None)
    pending = {
        "id": "77",
        "fileName": "交付看板按链接导出251111120000",
        "status": 1,
        "downloadFile": "",
    }
    ready = dict(pending)
    ready["status"] = 2
    ready["downloadFile"] = "https://files.example/link.xlsx"
    client = _Client(
        rows_by_call=[[], [pending], [pending], [pending], [pending], [ready]],
        export_result={"taskId": "77", "fileName": pending["fileName"]},
    )

    created = json.loads(
        delivery_export.export_delivery_result(
            client,
            {
                "mode": "link",
                "project_name": "杜甫江阁",
                "export_template_id": "12",
                "confirm": True,
            },
        )
    )
    assert created["download_url"] == ""
    assert created["task_id"] == "77"
    export_payload = [
        payload for path, payload in client.calls if path.endswith("/link/export")
    ][0]
    assert export_payload["exportTemplateId"] == "12"

    finished = json.loads(
        delivery_export.get_download_task(client, {"task_id": "77"})
    )
    assert finished["download_url"] == "https://files.example/link.xlsx"
    assert finished["task_id"] == "77"
