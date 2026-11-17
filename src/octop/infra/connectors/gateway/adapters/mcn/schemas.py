"""Declarative MCP tool definitions and write-tool classification for MCN."""

from __future__ import annotations

from typing import Any

from octop.infra.auth.youli.delivery_export import (
    EXPORT_DESCRIPTION,
    GET_DOWNLOAD_TASK_DESCRIPTION,
    LIST_DELIVERY_PROJECTS_DESCRIPTION,
    LIST_EXPORT_TEMPLATES_DESCRIPTION,
)


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _page_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
    *,
    page_size_default: int = 20,
) -> dict[str, Any]:
    return _schema(
        {
            **properties,
            "page": {"type": "integer", "description": "页码，从 1 开始"},
            "page_size": {
                "type": "integer",
                "description": f"每页数量，默认 {page_size_default}，最大 100",
            },
        },
        required,
    )


def _confirm_schema(
    properties: dict[str, Any],
    required: list[str],
    *,
    confirm_desc: str,
    summary_desc: str = "简短中文摘要，供确认弹窗展示",
) -> dict[str, Any]:
    return _schema(
        {
            **properties,
            "confirm": {"type": "boolean", "description": confirm_desc},
            "confirm_summary": {"type": "string", "description": summary_desc},
        },
        required,
    )


TOOLS: list[dict[str, Any]] = [
    {
        "name": "youli_list_accounts",
        "description": "查询经贝 MCN 账号库，拉包报价先选账号时使用。返回 items 和 page 分页信息。",
        "inputSchema": _page_schema(
            {
                "account_name": {"type": "string", "description": "账号名称关键字，可空"},
                "platform": {
                    "type": "integer",
                    "description": "媒体平台：1抖音 2快手 3小红书 4微博 5微信 6今日头条 7B站；0 表示不限",
                },
            }
        ),
    },
    {
        "name": "youli_query_account_specs",
        "description": "批量查询所选账号的可用规格与价格，拉包报价第二步使用。",
        "inputSchema": _schema(
            {
                "product_id": {"type": "string", "description": "产品 ID"},
                "account_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "账号 ID 列表，最多 50 个",
                },
                "version_id": {"type": "string", "description": "价格版本 ID，可空"},
            },
            ["product_id", "account_ids"],
        ),
    },
    {
        "name": "youli_list_quotes",
        "description": "查询经贝 MCN 报价单列表。返回 items 和 page 分页信息。",
        "inputSchema": _page_schema(
            {"keyword": {"type": "string", "description": "报价单名称或编号关键字，可空"}},
            page_size_default=10,
        ),
    },
    {
        "name": "youli_get_quote",
        "description": "读取经贝 MCN 报价单详情，含明细、账号、规格、数量和金额。",
        "inputSchema": _schema({"quote_id": {"type": "string"}}, ["quote_id"]),
    },
    {
        "name": "youli_list_products",
        "description": "查询经贝 MCN 产品列表，用于确定报价单的 productId。返回 items 和 page 分页信息。",
        "inputSchema": _page_schema(
            {"keyword": {"type": "string", "description": "产品名称关键字，可空"}}
        ),
    },
    {
        "name": "youli_list_customers",
        "description": "查询经贝 MCN 客户主数据（公司客户库），按当前员工的客户管理数据权限过滤，用于创建报价/合同时确定 customer_id。返回 items 和 page 分页信息。查询我的客户、客户归属或客户信息时请用 youli_list_customer_ownerships。",
        "inputSchema": _page_schema(
            {"keyword": {"type": "string", "description": "客户名称关键字，可空"}}
        ),
    },
    {
        "name": "youli_list_customer_ownerships",
        "description": "查询当前员工数据权限内的客户归属（我的客户/客户商机），用于查客户归属、我的客户或客户信息。返回 items（客户归属ID、客户ID、客户名称、类别、联系人、电话、归属商务/执行等）和 page 分页信息。",
        "inputSchema": _page_schema(
            {
                "keyword": {"type": "string", "description": "客户名称关键字，可空"},
                "customer_id": {"type": "string", "description": "客户ID，可空"},
            }
        ),
    },
    {
        "name": "youli_list_contracts",
        "description": "查询经贝 MCN 销售合同。返回 items 和 page 分页信息。",
        "inputSchema": _page_schema(
            {
                "keyword": {"type": "string", "description": "合同名称或客户关键字，可空"},
                "customer_id": {"type": "string", "description": "客户 ID，可空"},
            }
        ),
    },
    {
        "name": "youli_get_contract",
        "description": "读取单份销售合同详情：类型、客户、商机、签约主体、有效期和保价版本。",
        "inputSchema": _schema({"contract_id": {"type": "string"}}, ["contract_id"]),
    },
    {
        "name": "youli_list_company_entities",
        "description": "查询当前机构可用的签约主体（开票单位），创建合同时必填 issuing_office。",
        "inputSchema": _schema({}),
    },
    {
        "name": "youli_list_opportunities",
        "description": "查询客户商机列表，创建合同/项目/报价时用于确定 customer_opportunity_id；返回 items（精简字段）和 page 分页信息。查询客户归属明细请用 youli_list_customer_ownerships。",
        "inputSchema": _page_schema(
            {
                "keyword": {"type": "string", "description": "客户或商机名称关键字，可空"},
                "customer_id": {"type": "string", "description": "客户 ID，可空"},
            }
        ),
    },
    {
        "name": "youli_list_dictionaries",
        "description": "查询业务字典（项目类型 project_type、平台 account_platform、合同类型 contract_type 等）。",
        "inputSchema": _schema(
            {
                "enum_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "字典 key 列表，可空；默认 project_type/account_platform/contract_type",
                }
            }
        ),
    },
    {
        "name": "youli_list_projects",
        "description": "查询客户项目列表（含项目类型、主平台、预算）。返回 items 和 page 分页信息。",
        "inputSchema": _page_schema(
            {
                "keyword": {"type": "string", "description": "项目名称关键字，可空"},
                "customer_opportunity_id": {"type": "string", "description": "客户商机 ID，可空"},
                "project_type": {"type": "integer", "description": "项目类型，可空"},
            }
        ),
    },
    {
        "name": "youli_list_price_versions",
        "description": "查询经贝 MCN 账号价格版本。返回 items 和 page 分页信息。",
        "inputSchema": _page_schema(
            {"keyword": {"type": "string", "description": "价格版本名称关键字，可空"}}
        ),
    },
    {
        "name": "youli_list_coupon_versions",
        "description": "查询经贝 MCN 优惠政策版本。返回 items 和 page 分页信息。",
        "inputSchema": _page_schema(
            {"keyword": {"type": "string", "description": "优惠政策版本名称关键字，可空"}}
        ),
    },
    {
        "name": "youli_parse_quote",
        "description": "把客户需求原文解析成结构化报价单草稿。",
        "inputSchema": _schema(
            {
                "raw_text": {"type": "string"},
                "product_id": {"type": "string"},
            },
            ["raw_text", "product_id"],
        ),
    },
    {
        "name": "youli_save_quote",
        "description": "保存经贝 MCN 报价单（写操作）。参数核对清晰后直接以 confirm=true 调用，系统会弹出确认弹窗由用户批准执行。",
        "inputSchema": _confirm_schema(
            {"payload": {"type": "object", "description": "OfferOrderSaveRo 结构的报价单"}},
            ["payload", "confirm"],
            confirm_desc="准备执行保存时传 true，系统自动触发交互确认弹窗",
            summary_desc="简短中文摘要，说明客户、产品和金额，供确认弹窗展示",
        ),
    },
    {
        "name": "youli_create_package_quote",
        "description": "创建账号报价：先用 confirm=false 生成只读草稿，自动带出散投项目类型、常规单、商机首个有效合同、合同保价或默认价格/优惠版本、账号规格与价格，并调用页面同一优惠试算。不要向用户索要 ID，不要逐个确认默认字段；若缺少客户、产品或 account_specs（账号/规格/数量）等必要字段，必须先查真实候选并持续调用 ask_user_question 分步引导补齐（禁止只发纯文本停等用户回复）。拿到试算草稿与 expected_amount 后直接以 confirm=true 调用，系统会自动弹出确认弹窗由用户批准保存（无需先再用文字问是否保存）。接口为空只说明当前查询无结果，不可断言接口坏了或系统没有其他记录。",
        "inputSchema": _confirm_schema(
            {
                "name": {"type": "string", "description": "报价单名称"},
                "product_id": {"type": "string"},
                "customer_id": {"type": "string"},
                "price_version_id": {
                    "type": "string",
                    "description": "用户指定时传；省略则自动按页面规则选择",
                },
                "coupon_version_id": {
                    "type": "string",
                    "description": "用户指定时传；省略则自动按页面规则选择",
                },
                "project_type": {
                    "type": "integer",
                    "description": "散投自动带出；项目产品需明确业务类型",
                },
                "order_type": {"type": "integer", "description": "默认常规单 1"},
                "contract_id": {
                    "type": "string",
                    "description": "可选，默认商机下首个有效已审核合同",
                },
                "expected_amount": {
                    "type": "number",
                    "description": "confirm=true 时传入草稿试算得到的应付金额；金额变化会阻止保存",
                },
                "customer_opportunity_id": {
                    "type": "string",
                    "description": "客户商机 ID，可空；传了报价和后续订单才能归到项目",
                },
                "account_specs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "每项包含 account_id、sku_id；price 可省略以取规格价格，quantity 默认 1。账号名、规格名、平台、运营方式自动查询；渠道存在多个供应商时指定 supplier_id。",
                },
            },
            ["name", "product_id", "customer_id", "account_specs", "confirm"],
            confirm_desc="试算草稿传入 false，正式保存传 true（触发确认弹窗）",
            summary_desc="简短中文摘要（客户、账号规格、数量与应付金额），供确认弹窗展示",
        ),
    },
    {
        "name": "youli_create_order_from_quote",
        "description": "从报价单创建并提交销售订单（写操作）。参数核对清晰后直接以 confirm=true 调用，系统会弹出确认弹窗由用户批准下单。",
        "inputSchema": _confirm_schema(
            {
                "quote_id": {"type": "string"},
                "project_id": {
                    "type": "string",
                    "description": "娱乐/品牌名对应的父项目 ID，可空；填了订单才能按项目统计交付",
                },
                "sub_project_id": {"type": "string", "description": "子项目 ID，可空"},
                "commit_type": {
                    "type": "integer",
                    "description": "下单类型：1水下单 2平台单-客户下单 3平台单-代客下单 4佣金单，默认1",
                },
            },
            ["quote_id", "confirm"],
            confirm_desc="准备下单时传 true，触发交互确认弹窗",
            summary_desc="简短中文下单摘要（报价单、项目、下单类型等），供确认弹窗展示",
        ),
    },
    {
        "name": "youli_create_combined_order",
        "description": "组合订单：按供应商把一张拉包报价拆成多张订单（写操作）。参数核对清晰后直接以 confirm=true 调用，系统会弹出确认弹窗由用户批准执行。",
        "inputSchema": _confirm_schema(
            {
                "quote_id": {"type": "string"},
                "project_id": {"type": "string", "description": "父项目 ID，可空"},
                "sub_project_id": {"type": "string", "description": "子项目 ID，可空"},
                "commit_type": {
                    "type": "integer",
                    "description": "下单类型：1水下单 2平台单-客户下单 3平台单-代客下单 4佣金单，默认1",
                },
            },
            ["quote_id", "confirm"],
            confirm_desc="准备创建组合订单时传 true，触发交互确认弹窗",
        ),
    },
    {
        "name": "youli_list_service_items",
        "description": "查询订单服务项目（账号+规格+发布链接）。返回 items 和 page 分页信息。",
        "inputSchema": _page_schema({"order_id": {"type": "string"}}, ["order_id"]),
    },
    {
        "name": "youli_update_publish_link",
        "description": "填写服务项目发布链接（写操作）。参数核对清晰后直接以 confirm=true 调用，系统会弹出确认弹窗由用户批准保存。",
        "inputSchema": _confirm_schema(
            {
                "service_item_id": {"type": "string"},
                "publish_link": {"type": "string"},
                "publish_time": {
                    "type": "string",
                    "description": "发布时间 yyyy-MM-dd；时分秒会被忽略",
                },
                "publish_text": {"type": "string", "description": "发布文案，可空（留空保留原值）"},
                "platform_order_id": {
                    "type": "string",
                    "description": "平台订单 ID，可空（留空保留原值）",
                },
            },
            ["service_item_id", "publish_link", "confirm"],
            confirm_desc="准备保存发布链接时传 true，触发交互确认弹窗",
        ),
    },
    {
        "name": "youli_export_delivery",
        "description": EXPORT_DESCRIPTION,
        "inputSchema": _confirm_schema(
            {
                "mode": {
                    "type": "string",
                    "description": "project 按项目，link 按每条发布链接。不要把英文值问给用户",
                },
                "project_name": {"type": "string"},
                "customer_opportunity_id": {"type": "string"},
                "export_template_id": {
                    "type": "string",
                    "description": "仅按链接导出且用户指定了模板名称时传入。不要问用户要 ID",
                },
            },
            ["mode", "confirm"],
            confirm_desc="参数明确后传 true，触发交互确认弹窗",
            summary_desc="简短中文导出摘要（项目名和导出方式），供确认弹窗展示",
        ),
    },
    {
        "name": "youli_list_delivery_projects",
        "description": LIST_DELIVERY_PROJECTS_DESCRIPTION,
        "inputSchema": _page_schema(
            {
                "keyword": {"type": "string", "description": "用户说的或截图上的项目名，可空"},
                "name_field": {
                    "type": "string",
                    "description": "project 查娱乐/品牌名，brand 查订单项目名",
                },
            },
            [],
        ),
    },
    {
        "name": "youli_list_export_templates",
        "description": LIST_EXPORT_TEMPLATES_DESCRIPTION,
        "inputSchema": _page_schema(
            {
                "template_name": {
                    "type": "string",
                    "description": "模板名称，可空表示列出启用的按链接模板",
                }
            },
            [],
            page_size_default=50,
        ),
    },
    {
        "name": "youli_get_download_task",
        "description": GET_DOWNLOAD_TASK_DESCRIPTION,
        "inputSchema": _schema(
            {
                "task_id": {"type": "string", "description": "导出返回的任务 ID"},
                "file_prefix": {
                    "type": "string",
                    "description": "没有任务 ID 时使用导出返回的 file_prefix",
                },
            },
            [],
        ),
    },
    {
        "name": "youli_create_project",
        "description": "创建客户父项目（写操作）。参数核对清晰后直接以 confirm=true 调用，系统会弹出确认弹窗由用户批准创建。子项目请用 youli_create_sub_project。",
        "inputSchema": _confirm_schema(
            {"project_name": {"type": "string"}},
            ["project_name", "confirm"],
            confirm_desc="准备创建时传 true，触发交互确认弹窗",
        ),
    },
    {
        "name": "youli_create_sub_project",
        "description": "创建客户子项目，支持不同项目类型（project_type）和主平台（main_platform）。参数核对清晰后直接以 confirm=true 调用，系统会弹出确认弹窗由用户批准创建。",
        "inputSchema": _confirm_schema(
            {
                "project_name": {"type": "string"},
                "customer_opportunity_id": {"type": "string"},
                "customer_name": {"type": "string"},
                "project_type": {
                    "type": "integer",
                    "description": "项目类型，先用 youli_list_dictionaries 查询 project_type",
                },
                "main_platform": {
                    "type": "integer",
                    "description": "主平台，1抖音 2快手 3小红书 4微博 5微信 6今日头条 7B站",
                },
                "project_budget": {"type": "number", "description": "项目预算，可空"},
                "project_leader_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "项目负责人员工 ID 列表，可空",
                },
                "parent_project_id": {"type": "string", "description": "父项目 ID，可空"},
            },
            [
                "project_name",
                "customer_opportunity_id",
                "customer_name",
                "project_type",
                "main_platform",
                "confirm",
            ],
            confirm_desc="准备创建时传 true，触发交互确认弹窗",
        ),
    },
    {
        "name": "youli_create_coupon_version",
        "description": "创建优惠政策版本，含客户特定版本（写操作）。参数核对清晰后直接以 confirm=true 调用，系统会弹出确认弹窗由用户批准创建。",
        "inputSchema": _confirm_schema(
            {
                "name": {"type": "string"},
                "version": {"type": "string"},
                "opportunity_id": {"type": "string"},
                "opportunity_name": {
                    "type": "string",
                    "description": "特定客户商机名称，传入 opportunity_id 时自动回填或显式传入",
                },
                "is_default": {"type": "boolean"},
                "effective_time": {"type": "string"},
                "expiration_time": {"type": "string"},
            },
            ["name", "confirm"],
            confirm_desc="准备创建时传 true，触发交互确认弹窗",
        ),
    },
    {
        "name": "youli_create_contract",
        "description": "创建销售合同（写操作）。参数核对清晰后直接以 confirm=true 调用，系统会弹出确认弹窗由用户批准创建。合同类型 type：1单次 2季框 3半年框 4年框；签约主体 issuing_office 先通过 youli_list_company_entities 获取。",
        "inputSchema": _confirm_schema(
            {
                "payload": {"type": "object", "description": "ContractRo 结构"},
                "name": {"type": "string", "description": "合同名称"},
                "customer_id": {"type": "string", "description": "签约客户 ID"},
                "customer_opportunity_id": {
                    "type": "string",
                    "description": "客户商机 ID；填了合同才会出现在合同列表",
                },
                "type": {"type": "integer", "description": "合同类型 1单次 2季框 3半年框 4年框"},
                "cooperation_mode": {
                    "type": "integer",
                    "description": "合作方式：1单次合作 2长线合作；缺省按合同类型自动推导",
                },
                "scale_amount": {"type": "number", "description": "签约规模（万元），可空"},
                "guaranteed_type": {
                    "type": "integer",
                    "description": "保量类型：1不保量 2固定保量 3梯度保量，可空",
                },
                "issuing_office": {"type": "string", "description": "签约主体/开票单位 ID"},
                "effective_time": {"type": "string", "description": "有效开始日期 yyyy-MM-dd"},
                "expiration_time": {"type": "string", "description": "有效截止日期 yyyy-MM-dd"},
                "signing_date": {"type": "string", "description": "签约日期 yyyy-MM-dd，可空"},
                "price_version_id": {"type": "string", "description": "保价价格版本 ID，可空"},
                "price_protect_type": {
                    "type": "integer",
                    "description": "保价类型：1普通保价 2特客直签 3特客复签，可空",
                },
                "price_start_time": {
                    "type": "string",
                    "description": "保价开始日期 yyyy-MM-dd，可空",
                },
                "price_end_time": {
                    "type": "string",
                    "description": "保价结束日期 yyyy-MM-dd，可空",
                },
                "coupon_version_id": {"type": "string", "description": "保价优惠版本 ID，可空"},
                "coupon_protect_type": {
                    "type": "integer",
                    "description": "保优惠类型：1普通保价 2特客直签 3特客复签，可空",
                },
                "favorable_start_time": {
                    "type": "string",
                    "description": "保优惠开始日期 yyyy-MM-dd，可空",
                },
                "favorable_end_time": {
                    "type": "string",
                    "description": "保优惠结束日期 yyyy-MM-dd，可空",
                },
                "employee_id": {"type": "string", "description": "签单人 ID，可空"},
                "user_name": {"type": "string", "description": "签单人姓名，可空"},
                "enable_type": {"type": "integer", "description": "1临时启用 2正式启用"},
                "contract_account_info": {
                    "type": "object",
                    "description": "账期信息，默认 {periodType:1}",
                },
                "remark": {"type": "string", "description": "合同备注，可空"},
            },
            ["confirm"],
            confirm_desc="准备创建合同时传 true，触发交互确认弹窗",
            summary_desc="简短中文合同摘要，供确认弹窗展示",
        ),
    },
]

_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "youli_save_quote",
        "youli_create_package_quote",
        "youli_create_order_from_quote",
        "youli_create_combined_order",
        "youli_update_publish_link",
        "youli_export_delivery",
        "youli_create_project",
        "youli_create_sub_project",
        "youli_create_coupon_version",
        "youli_create_contract",
    }
)
