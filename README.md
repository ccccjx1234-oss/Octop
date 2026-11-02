<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="dashboard/public/logo_horizontal_white.png" />
    <img src="dashboard/public/logo_horizontal_dark.png" alt="会火 Huihuo" width="360" />
  </picture>
</p>

<p align="center">
  <strong>A self-hosted AI assistant platform for teams and enterprises.</strong>
</p>

---

**会火 (Huihuo)** is a self-hosted AI assistant platform for teams and enterprises. One process serves a web console, a CLI, IM channels, scheduled jobs, and an HTTP/WebSocket API. Conversations, workspaces, and credentials stay on your own infrastructure.

Use the web console, Feishu, DingTalk, QQ, WeChat, WeCom, Telegram, Discord, or the API to talk to your agents. Extend them with the expert library, connectors (OAuth + MCP), knowledge bases, browser automation, a terminal, and remote desktop control.

## Highlights

| | Feature | Description |
|---|---------|-------------|
| 👥 | **Multi-user workspace** | One admin, multiple members; each user gets isolated agents, workspaces, providers, channels, and scheduled jobs |
| 🎭 | **Expert team** | Built-in expert library, 16 MBTI persona templates, and a coordinator that schedules multiple experts on multi-step work |
| 🔌 | **Connectors** | OAuth and MCP connectors for Feishu, DingTalk, WeCom, WeChat, Tencent Docs, Notion, and more — including custom MCP servers such as ERP |
| 📚 | **Knowledge base** | RAG over private documents, shareable within one deployment |
| 💾 | **Pluggable storage** | Local disk, Docker sandbox, PostgreSQL, or S3/COS for agent files, separate from the control-plane database |
| 🧠 | **Portable memory** | Agent memory travels with the workspace and survives restarts |
| 💻 | **Browser, terminal, remote desktop** | Headless Chromium sessions, an in-browser shell, and live desktop control from the dashboard |
| 🏠 | **Self-hosted** | Dashboard, CLI, IM channels, and cron in one process; SQLite by default, PostgreSQL optional |

## Architecture

```
Browser / IM / CLI
        │
   FastAPI + WebSocket / SSE
        │
   Agent runtime (LangGraph)
        │
SQLite / PostgreSQL + workspace files
```

The whole stack runs in one process. There is no external queue or worker; state is rebuilt from the control-plane database on boot.

> The service CLI entry point is still named `octop` for compatibility. Runtime data lives under `~/.octop/`.

## Quick start

### From source

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run octop init
uv run octop run --host 0.0.0.0 --port 8088
```

Open **http://127.0.0.1:8088** and complete the first-run wizard.

### Docker

```bash
docker build -f docker/Dockerfile -t octop:latest .
docker run -d --name huihuo \
  -p 8088:8088 \
  -v octop-data:/data/.octop \
  -e HOME=/data \
  octop:latest
```

For the full team stack (PostgreSQL + S3-compatible storage + reverse proxy), see [docker/README-team-stack.md](docker/README-team-stack.md).

### Serve under a subpath

To publish the dashboard at `https://example.com/octop/`, build the frontend with `VITE_BASE_PATH=/octop/` and strip the prefix in your reverse proxy.

## Configuration

Runtime state lives in `~/.octop/`.

```bash
octop provider list          # LLM providers and models
octop channel list           # IM channels
octop cron list              # scheduled jobs
octop user list              # users (admin)
octop skills list --agent main
```

Providers, channels, connectors, knowledge bases, and cron jobs can also be managed from the dashboard.

### Supported LLM providers

OpenAI-compatible APIs, Anthropic, AWS Bedrock, DashScope (Qwen), Ollama, and other presets. Configure per agent in the dashboard or with `octop provider`.

### Supported channels

| Channel | Credentials |
|---------|-------------|
| Feishu | App ID, App Secret |
| DingTalk | App Key, App Secret |
| QQ | Bot AppID, Token |
| WeChat | QR bind or account credentials |
| WeCom | Corp ID, Agent Secret |
| Telegram | Bot Token |
| Discord | Bot Token |
| Web dashboard | Enabled by default |

### Connectors

Built-in connectors cover Feishu, DingTalk, WeCom, Tencent Docs, Tencent Meeting, Notion, TickTick, OpenAlex, Obsidian, Dify, and more. Custom MCP servers can be added from the dashboard — for example an ERP service exposed over MCP.

Full catalog: [docs/connectors.md](docs/connectors.md).

## Data directory

```
~/.octop/
├── config.json                    # process config
├── octop.db                       # SQLite control plane
├── secrets/                       # JWT secret and channel tokens
├── agents/<agent_id>/             # per-agent workspace
├── security/tool_guard/           # shell command rules
├── logs/
├── venv/                          # uv-managed Python (installer layout)
└── bin/octop                      # CLI wrapper
```

PostgreSQL can replace SQLite by setting `database` in `config.json` or the `OCTOP_DATABASE_*` environment variables. See [docs/configuration.md](docs/configuration.md).

## Security

- **Local-first**: config, chats, workspaces, and credentials stay under `~/.octop/`.
- **Multi-user isolation**: JWT authentication with per-user agents and workspaces.
- **Tool approval and guardrails**: sensitive data is redacted before it leaves the workspace; risky tools and shell commands require explicit approval.
- **No vendor lock-in**: swap LLM providers, storage backends, and channels without rewriting agents.

## Documentation

- [User guide](docs/user-guide.md)
- [CLI reference](docs/cli.md)
- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Connectors](docs/connectors.md)
- [ACP integration](docs/acp.md)
- [LDAP](docs/ldap.md)
- [Memory maintenance](docs/memory-slim.md)

## License

This project is licensed under the [MIT License](LICENSE).
