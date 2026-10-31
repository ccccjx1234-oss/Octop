# Octop 团队栈（PostgreSQL + SeaweedFS S3 + Caddy）

`docker-compose.team.yml` 把 Octop 拆成四个服务，只有反向代理对外发布端口：

| 服务 | 镜像 | 暴露 | 说明 |
|------|------|------|------|
| `caddy` | caddy:2-alpine | `127.0.0.1:8443`（HTTPS） | TLS 终止、安全响应头、反向代理 |
| `octop` | `octop:local`（本仓库构建） | 仅 compose 网络 | 应用本体 |
| `postgres` | pgvector/pgvector:pg16 | 仅 compose 网络 | 控制面数据库（含 agent memory schema） |
| `seaweedfs` | chrislusf/seaweedfs | Master UI `127.0.0.1:9333` | S3 兼容对象存储（智能体工作区） |

> MinIO 官方镜像已从 Docker Hub 下架、quay.io 匿名拉取返回 401，因此这里用
> SeaweedFS 的 S3 网关（成熟、持续维护，path-style 寻址与 Octop 兼容）。
> 如果公司已有 S3 / COS / OSS，直接在管理后台配置对应存储后端即可，不需要此服务。

`seaweedfs-init` 是一次性初始化容器：等集群就绪后创建 bucket。

## 启动

```bash
# 1) 构建镜像（源码改动后重新执行）
docker build -f docker/Dockerfile -t octop:local .

# 2) 密钥文件（仓库外，切勿提交），本机已生成：
#    D:/octop-stack/octop-stack.env
#    其中包含 POSTGRES_PASSWORD / S3_APP_SECRET / OCTOP_DEFAULT_PASSWORD 等

# 3) 启动 / 更新
docker compose --env-file D:/octop-stack/octop-stack.env \
  -f docker/docker-compose.team.yml up -d

# 4) 查看
docker compose --env-file D:/octop-stack/octop-stack.env \
  -f docker/docker-compose.team.yml ps
docker logs -f octop-stack-app
```

访问 `https://127.0.0.1:8443`。Caddy 使用自签内部 CA（`tls internal`），浏览器会告警；
内网试用可以接受，正式域名请修改 `docker/caddy/Caddyfile` 的站点地址和 `tls` 行
（例如 `octop.example.com { tls admin@example.com }`），并把 `tls internal` 删掉。

服务端只在 compose 网络监听 8088，PostgreSQL 与 SeaweedFS 的 S3 API 都不发布到宿主机。

## 数据位置

- PostgreSQL：Docker 命名卷 `octop-stack_pgdata`
- 对象存储：命名卷 `octop-stack_seaweeddata`
- Octop 宿主侧文件：`${OCTOP_DATA_DIR}`（默认 `D:/octop-stack/octop-data`），
  包括知识库、技能包、每个专家的会话/记忆/检查点
- 使用 S3 工作区时，专家的 `AGENTS.md`、上传件、产物在 bucket
  `octop-workspaces` 中；宿主工作区仍保留会话与检查点

## 重要前提：PostgreSQL 是全新实例

ADR 002 明确说明没有 SQLite→PostgreSQL 的数据迁移器，备份也不能跨引擎恢复。
本栈首次启动会创建空库；已有的 SQLite 实例（`octop-local` / `D:\octop-data-local`）
不会被迁移。若需要保留旧对话，请继续使用旧实例，或另行评估一次性搬运脚本。

## 配置 SeaweedFS 工作区

登录后进入「管理 → 存储」，新建：

| 字段 | 值 |
|------|----|
| 名称 | `seaweedfs-workspaces` |
| 类型 | S3 |
| Endpoint | `http://seaweedfs:8333`（必须带协议） |
| Access key | `S3_APP_KEY`（env 文件中的值，默认 `octopapp`） |
| Secret key | `S3_APP_SECRET` |
| Bucket | `S3_BUCKET`（默认 `octop-workspaces`） |

然后在专家设置里把 backend 改为 `named` 引用该后端，或直接写：

```json
{ "backend": { "type": "named", "name": "seaweedfs-workspaces" } }
```

可用「探测」或 `/api/admin/storage-backends/probe` 验证连通性。

## 备份

- PostgreSQL：`docker exec octop-stack-postgres pg_dump -U octop -Fc octop > octop.dump`
- 对象存储：用 `rclone` 或 `aws s3 sync --endpoint-url http://127.0.0.1:...` 对
  `octop-workspaces` 做镜像（S3 API 默认不发布，可按需临时映射端口或从容器网络内运行）
- 宿主文件：打包 `OCTOP_DATA_DIR`
- 备份必须加密后异地保存——默认备份/转储都是明文，内含全部凭据与对话。

## 安全边界

- 只有 Caddy 发布端口；数据库与对象存储只在 compose 网络内，密码使用生成的强口令；
- Octop 容器仍以 root 运行，所有租户共享同一个容器与数据目录；本栈解决的是
  “服务拆分与外部化存储”，不是租户隔离。需要强隔离时按团队/实例拆分部署；
- 自定义 MCP、共享专家工作区读取等已知问题仍在，详见安全审查结论；
- 移动端 Redroid compose 使用了 privileged + host 网络 + docker.sock，本栈不包含。
