<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="dashboard/public/logo_horizontal_white.png" />
    <img src="dashboard/public/logo_horizontal_dark.png" alt="会火" width="360" />
  </picture>
</p>

<p align="center">
  <strong>面向团队与企业的自托管 AI 助手平台。</strong>
</p>

---

**会火** 是面向团队与企业的自托管 AI 助手平台。单进程同时提供 Web 控制台、CLI、IM 通道、定时任务与 HTTP/WebSocket API，对话、工作区与凭据都保存在你自己的基础设施里。

你可以通过 Web 控制台、飞书、钉钉、QQ、微信、企业微信、Telegram、Discord 或 API 与专家对话；也可以通过专家库、连接器（OAuth + MCP）、知识库、浏览器自动化、终端与远程桌面扩展能力。

## ✨ 亮点

| | 特性 | 说明 |
|---|------|------|
| 👥 | **多用户工作区** | 一个管理员管理多个成员；每个用户拥有独立的专家、工作区、供应商、通道与定时任务 |
| 🎭 | **专家团** | 内置专家库、16 种 MBTI 人格模板，协调者可以调度多位专家完成多步骤任务 |
| 🔌 | **连接器** | 内置飞书、钉钉、企业微信、微信、腾讯文档、Notion 等 OAuth / MCP 连接器，也支持 ERP 等自定义 MCP 服务 |
| 📚 | **知识库** | 基于私有文档的 RAG 检索，可在同一部署内共享 |
| 💾 | **可插拔存储** | 本地磁盘、Docker 沙箱、PostgreSQL 或 S3/COS 存放专家文件，与控制面数据库分离 |
| 🧠 | **可迁移记忆** | 记忆随工作区保存，重启后仍然可用 |
| 💻 | **浏览器 / 终端 / 远程桌面** | 无头 Chromium 会话、浏览器内 Shell，以及在控制台实时操控桌面 |
| 🏠 | **自托管** | 控制台、CLI、IM 通道与定时任务同进程运行；默认 SQLite，可选 PostgreSQL |

## 🏗️ 架构

```
浏览器 / IM / CLI
        │
   FastAPI + WebSocket / SSE
        │
   Agent 运行时（LangGraph）
        │
SQLite / PostgreSQL + 工作区文件
```

整个栈以单进程运行，不需要外部队列或 Worker；启动时从控制面数据库重建状态。

> 服务命令行入口为兼容底层运行时仍叫 `octop`，运行数据保存在 `~/.octop/`。

## 🚀 快速开始

### 源码运行

需要 Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run octop init
uv run octop run --host 0.0.0.0 --port 8088
```

打开 **http://127.0.0.1:8088**，按首次启动向导完成初始化。

### Docker

```bash
docker build -f docker/Dockerfile -t octop:latest .
docker run -d --name huihuo \
  -p 8088:8088 \
  -v octop-data:/data/.octop \
  -e HOME=/data \
  octop:latest
```

完整团队栈（PostgreSQL + S3 兼容存储 + 反向代理）见 [docker/README-team-stack.md](docker/README-team-stack.md)。

### 部署到子路径

如果要把控制台发布在 `https://example.com/octop/`，构建前端时设置 `VITE_BASE_PATH=/octop/`，并在反向代理中剥离该前缀。

## ⚙️ 配置

运行状态保存在 `~/.octop/`。

```bash
octop provider list          # 大模型供应商
octop channel list           # IM 通道
octop cron list              # 定时任务
octop user list              # 用户（管理员）
octop skills list --agent main
```

供应商、通道、连接器、知识库与定时任务也可以在控制台中管理。

### 支持的大模型供应商

OpenAI 兼容 API、Anthropic、AWS Bedrock、DashScope（通义千问）、Ollama 等。可在控制台中按专家配置，也可使用 `octop provider`。

### 支持的通道

| 通道 | 凭据 |
|------|------|
| 飞书 | App ID、App Secret |
| 钉钉 | App Key、App Secret |
| QQ | Bot AppID、Token |
| 微信 | 扫码绑定或账号凭据 |
| 企业微信 | Corp ID、Agent Secret |
| Telegram | Bot Token |
| Discord | Bot Token |
| Web 控制台 | 默认启用 |

### 连接器

内置飞书、钉钉、企业微信、腾讯文档、腾讯会议、Notion、TickTick、OpenAlex、Obsidian、Dify 等连接器。也可以在控制台中添加自定义 MCP 服务，例如通过 MCP 暴露的 ERP 系统。

完整目录见 [docs/connectors.md](docs/connectors.md)。

## 📁 数据目录

```
~/.octop/
├── config.json                    # 进程配置
├── octop.db                       # SQLite 控制面数据库
├── secrets/                       # JWT 密钥与通道 Token
├── agents/<agent_id>/             # 每个专家的工作区
├── security/tool_guard/           # Shell 命令规则
├── logs/
├── venv/                          # uv 管理的 Python（安装器布局）
└── bin/octop                      # CLI 包装器
```

在 `config.json` 中设置 `database`，或使用 `OCTOP_DATABASE_*` 环境变量，可以切换到 PostgreSQL。详见 [docs/configuration.md](docs/configuration.md)。

## 🔒 安全

- **本地优先**：配置、对话、工作区与凭据都保存在 `~/.octop/`。
- **多用户隔离**：JWT 认证，每个用户拥有独立的专家与工作区。
- **工具审批与护栏**：敏感信息离开工作区前会脱敏；高风险工具与 Shell 命令需要显式审批。
- **不绑定供应商**：可以更换大模型供应商、存储后端与通道，不需要重写专家。

## 📚 文档

- [用户指南](docs/user-guide.md)
- [CLI 参考](docs/cli.md)
- [配置](docs/configuration.md)
- [架构](docs/architecture.md)
- [连接器](docs/connectors.md)
- [ACP 集成](docs/acp.md)
- [LDAP](docs/ldap.md)
- [记忆维护](docs/memory-slim.md)

## 📄 许可证

本项目采用 [MIT License](LICENSE)。
