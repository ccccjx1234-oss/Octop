# 代码审查修复测试报告（2026-11-01）

- 分支：`feat/wiki-reliability-quality-permissions`
- 基线提交：`75a24ea0`（`chore(ui): drop version badge from sidebar and header`）
- 范围：`/code-review` 报告的 15 项发现，以及修复过程中确认的关联问题
- 状态：全部修复完成，未提交、未推送

## 结论

后端全量测试通过：**5015 passed, 225 skipped, 0 failed（295.75s）**（0 失败）。静态检查（ruff、mypy）与前端类型检查、构建全部通过。

前端全量测试为 **1443 通过 / 14 失败（9 个测试文件）**。这些失败在修复前的 HEAD 上以完全相同的文件、相同的用例复现（把修复 `git stash` 后重跑得到同样的 14 个失败），因此均为**预先存在**的问题，与本次修复无关；本次修复**没有新增任何前端失败**。

## 一、15 项审查发现的修复与验证

| # | 发现 | 修复 | 验证 |
|---|------|------|------|
| 1 | `task` 委派的子智能体不继承工具权限中间件，可绕过工具限制 | `tool_permission.py` 在用户存在任一受限工具授权时拒绝 `task` 委派；授权齐全或管理员不受影响；新增中英文提示 `quality.tool_delegation_denied` | `test_task_delegation_requires_all_restricted_tools` |
| 2 | 降级链急切解析全部链项，任一失效引用会拖垮所有指定模型回合 | `model_failover.py` 的 `_plan` 逐项解析链模型，失败的引用记录 warning 后跳过，主模型健康时仍可正常回答 | `test_unavailable_chain_ref_is_skipped` |
| 3 | 输出守卫把问句改写成 `ask_user_question`，cron/评测把 HITL 当失败 | 新增 `unattended` 请求标志（`octop_unattended`）：cron 投递与评测调用传 `unattended=True`，此时输出守卫不再注入 `ask_user_question` | `test_harness_request_marks_unattended`、`test_unattended_run_keeps_question_as_text` |
| 4 | 反馈使用前端临时 message id，刷新后点赞丢失、重复计数 | 后端 `resolve_feedback_target()` 按服务端 message id 或「已存文本前缀」解析目标，落库统一使用服务端 id 与实际文本 | `test_feedback_resolves_live_message_by_excerpt`、集成测试 `test_feedback_round_trip_and_admin_summary` |
| 5 | 评价接口完全信任客户端 thread/message/excerpt，可伪造投票与摘录 | 写入前校验线程存在、`thread.agent_id == agent_id`、`thread.user_id == 当前用户`；未知消息返回 404（清除投票 `rating=0` 保持幂等 204）；摘录改为从服务端消息生成 | `test_feedback_rejects_unknown_message`、`test_feedback_rejects_another_users_thread` |
| 6 | 默认备份的聊天排除列表缺少 `message_feedback` / `eval_results` | `backup/chats.py` 将两表加入 `CHAT_TABLES_CHILD_FIRST`，`include_chats=False` 时一并清除 | 扩展 `test_sqlite_strip_chats_reclaims_pages`（断言两表行数为 0） |
| 7 | 换牌后 business-operations 技能改编声明测试变红 | 测试断言同步为「已为会火改编」 | 全量 pytest 通过 |
| 8 | 降级链只按熔断健康度过滤，不检查模型用户组授权 | `SafeModelFailoverMiddleware` 新增 `ref_allowed`；`manager.py` 用当前请求用户调用 `model_ref_allowed_for_user()` 过滤链项 | `test_chain_ref_denied_by_user_grant_is_not_tried` |
| 9 | 团队栈 DSN 原样拼接密码，特殊字符导致解析失败 | Compose 改用 `OCTOP_DATABASE_HOST/PORT/NAME/USER/PASSWORD` 离散变量，由配置层安全编码 | `test_postgresql_conninfo_percent_encodes_password`（密码 `pa/ss@word%#`）、`docker compose config -q` |
| 10 | 质量接口把参数校验失败包成 `FORBIDDEN`，400 响应显示「Permission denied.」 | 新增 `ErrorCode.INVALID_ARGUMENT`（HTTP 400），补充后端与前端 i18n；反馈、降级链、评测、工具名、模型价格校验全部改用该错误码 | `test_feedback_rejects_invalid_rating_with_argument_code`、`test_save_chain_rejects_unusable_ref`、`test_fallback_chain_validation_uses_argument_code`、`test_unpriced_model_is_flagged` |
| 11 | 团队栈 CSP 阻止 Monaco（jsDelivr）与 `blob:` 插件 UI | Caddyfile 的 `script-src`/`worker-src`/`style-src`/`connect-src`/`font-src` 增加 `blob:` 与 `https://cdn.jsdelivr.net`，并更新注释 | `caddy validate` 输出 `Valid configuration` |
| 12 | 旁白清理正则误删带数据的第一行 | 收紧 `_NARRATION` 首条规则（必须包含叙述性动词子句）；头部空白行不再消耗旁白行数预算 | `test_data_bearing_result_line_is_kept`、`test_leading_blank_lines_do_not_hide_narration` |
| 13 | 成本报表漏算 `cache_write_tokens` | `UsageRepo.totals_by_user_model` 查询 `SUM(cache_write_tokens)`；`pricing.row_cost` 按输入单价计费（无独立缓存写入价） | `test_cost_report_counts_cache_write_tokens` |
| 14 | 成本区间切换重拉全部数据并覆盖未保存的降级链，且存在请求竞态 | `ModelsPanel` 拆分 `loadBase` / `loadCost`：区间切换只刷新成本；基础数据与成本请求各自用序号丢弃过期响应；仅首次加载用服务端值重置链 | `npx tsc -b`、`npm run build`（无专门前端单测） |
| 15 | 登录面板固定 `min-height:620px`，矮窗口被裁切且无法滚动 | `.shell` 改为 `height:100dvh + overflow-y:auto`；`max-height:720px` 媒体查询下 `.panel { min-height:0 }` | `npx tsc -b`、`npm run build`（CSS 无自动化视觉测试） |

## 二、关联修复（修复过程中确认）

- 删除会话时级联删除该会话的 `message_feedback` 行（`api/routers/chat/history.py`），避免评价残留。
- `FeedbackButtons`：仅在提交成功后写入本地评分缓存；初始 GET 返回前用户已操作时忽略旧值，避免回滚刚点的评价。
- `FeedbackPanel`：请求增加 `cancelled` 标志，切换时间区间时丢弃过期响应。
- `EvalsPanel`：轮询依赖改为布尔 `hasRunning`，避免 `runs` 每次更新都重建定时器。
- 评测模型解析改用 `resolve_explicit_default_model()`，解析不到再回退，避免直接读取 `row.default_model` 绕过配置。
- 质量面板统一使用 `apiErrorMessage()`；新增 `apiErrors.INVALID_ARGUMENT`、`quality.common.requestFailed`、`quality.tools.groupOff` 文案；停用分组标签本地化。
- 清理换牌遗留：删除无引用的版本徽标组件、无用 locale 键，`Sidebar` 冗余 Fragment 简化。

## 三、测试结果

### 3.1 后端

```bash
PYTHONUTF8=1 uv run pytest -n 8 -m "not live"
```

结果：**5015 passed, 225 skipped, 0 failed（295.75s）**。

> 说明：本机为中文 Windows，默认 GBK 会让测试读取 UTF-8 文件时报解码错误，因此统一加 `PYTHONUTF8=1`（CI 的 Linux/Windows 环境不受此影响）。

### 3.2 前端

```bash
cd dashboard && npx vitest run
```

本次修复后：**1443 passed / 14 failed（9 个文件）**。

基线对比（关键证据）：把全部修复 `git stash` 后在 HEAD 上运行同一套前端测试，结果为 **1443 passed / 14 failed，失败文件与用例完全相同**；`git stash pop` 恢复修复后再跑，结果仍为 1443/14。即本次修复未引入任何新失败。

预先存在的 9 个失败文件及原因：

| 文件 | 失败数 | 原因 |
|------|--------|------|
| `src/api/modules/knowledgeBases.test.ts` | 1 | 测试对 `../request` 的 mock 未提供 `bridgeAgentHeaders` |
| `src/api/modules/publishedExperts.test.ts` | 1 | 测试期望 refresh POST 无 body，实际实现已带 body |
| `src/components/DocumentPreviewCore.docxSanitize.test.ts` | 文件级 | jsdom 无 `DOMMatrix`（`ReferenceError`） |
| `src/layouts/sidebarNavLayout.test.ts` | 1 | 导航可见性预期与当前实现不一致（期望 `settings` 组） |
| `src/pages/Agent/Skills/components/SkillDrawer.test.ts` | 文件级 | jsdom 无 `DOMMatrix` |
| `src/pages/Agent/Skills/skillMarkdown.test.ts` | 文件级 | jsdom 无 `DOMMatrix` |
| `src/pages/Chat/components/ChatInput.prefill.test.tsx` | 4 | `chatStore` mock 缺少 `onSteerRestore` 导出 |
| `src/pages/Chat/hooks/useChat.history.test.ts` | 6 | 同上（`chatStore` mock 缺少 `onSteerRestore`） |
| `src/pages/Settings/Models/components/modals/ModelListEditor.test.tsx` | 1 | 断言文案 `models.purposeChat` 与当前渲染不符 |

这些文件均不在本次修改范围内，属于分支上已有的测试与实现漂移，未在本次修复中处理。

### 3.3 静态检查

| 检查 | 命令 | 结果 |
|------|------|------|
| Ruff lint | `uv run ruff check src tests` | All checks passed |
| Ruff format | `uv run ruff format --check src tests` | 1280 files already formatted |
| mypy | `uv run mypy --strict src/octop` | Success: no issues found in 622 source files |
| 前端类型 | `cd dashboard && npx tsc -b` | 通过 |
| 前端构建 | `cd dashboard && npm run build` | 构建成功（PWA precache 107 entries） |
| ESLint（质量面板） | `cd dashboard && npx eslint src/pages/Admin/Quality` | 0 error / 0 warning |
| Prettier（改动文件） | `npx prettier --check --end-of-line crlf <改动文件>` | All matched files use Prettier code style |

> 本仓库在 Windows 上以 CRLF 检出，Prettier 默认按 LF 比较会把整文件报为格式不符；使用 `--end-of-line crlf` 才是内容层面的真实检查，结果通过。

### 3.4 部署配置

```bash
docker compose --env-file D:/octop-stack/octop-stack.env -f docker/docker-compose.team.yml config -q
```

结果：退出码 0。

```bash
MSYS_NO_PATHCONV=1 docker run --rm -e OCTOP_SITE_HOST=localhost \
  -v D:/myProjcet/Octop/docker/caddy/Caddyfile:/etc/caddy/Caddyfile:ro \
  caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

结果：`Valid configuration`。

## 四、新增与扩展的测试

新增 15 个用例：

- `tests/unit/quality/test_quality.py`：缓存写入计费、按摘录解析反馈目标、非法评分错误码、`task` 委派权限
- `tests/unit/agents/test_model_fallback_chain.py`：失效链项跳过、用户组授权过滤、保存时拒绝不可用引用
- `tests/unit/agents/test_output_guard.py`：数据行保留、前导空行不遮蔽旁白、无人值守不注入 HITL
- `tests/unit/gateway/test_message_build.py`：`unattended` 标志写入请求
- `tests/unit/test_config.py`：PostgreSQL conninfo 密码编码
- `tests/integration/test_quality_api.py`：未知消息 404/清除幂等、他人线程 404、降级链校验错误码

扩展既有测试：

- `tests/unit/backup/test_system_archive.py`：断言备份剔除 `message_feedback` 与 `eval_results`
- `tests/integration/test_quality_api.py::test_feedback_round_trip_and_admin_summary`：预置真实线程与消息
- `tests/unit/quality/test_quality.py::test_unpriced_model_is_flagged`：价格校验断言 `INVALID_ARGUMENT`
- `tests/unit/agents/test_business_operations_template.py`：改编声明断言改为会火

## 五、已知限制

- 发现 8 只修复了降级链的**用户组授权**绕过；多模态/上下文窗口兼容性仍只在主候选选择中考虑，链式回退未增加额外约束。若要严格限制，需要模型能力元数据，本次未扩展以免过度设计。
- 发现 14（前端请求竞态）与发现 15（登录页矮窗口）没有专门的前端自动化测试，只有类型检查、构建与代码审查覆盖。
- 删除会话级联清理反馈的路径没有独立测试，只有实现与代码审查覆盖。
- 前端 14 个预先存在的失败未在本次范围内修复。

## 六、复现命令

```bash
PYTHONUTF8=1 uv run pytest -n 8 -m "not live"
```

```bash
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy --strict src/octop
```

```bash
cd dashboard && npx vitest run
```

```bash
cd dashboard && npx tsc -b && npm run build
```

前端基线复现：

```bash
git stash push -m baseline && (cd dashboard && npx vitest run); git stash pop
```
