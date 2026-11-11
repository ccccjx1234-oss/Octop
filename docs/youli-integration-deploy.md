# Octop × 经贝 MCN 接入部署说明

维护作者：chenjixian

本文件只记录可执行的接入配置，方案与验收口径见 [mcn-creator-integration-plan.md](mcn-creator-integration-plan.md)。

## 1. 数据库

CRM 库执行：

```bash
mysql -h <host> -u <user> -p xgj_youli_crm_sit < huihuo-mcn/DB/v3.8.0/02_xgj_youli_crm_youli_sso.sql
```

Octop 控制面执行迁移（SQLite 或 PostgreSQL 自动选择）：

```bash
octop db migrate
```

新增表：`youli_login_request`、`youli_session`、`youli_agent_binding`、`youli_business_template`。

## 2. 应用登记

在 CRM 库插入一条应用记录，密钥只存 SHA-256：

```sql
-- secret 使用 openssl rand -hex 32 生成；哈希使用 sha256sum
INSERT INTO youli_oauth_client
  (client_id, secret_hash, audience, callback_uri, enabled)
VALUES
  ('octop', '<sha256-of-secret>', 'octop',
   'https://8.163.15.131/aiwork/login/youli/complete', 1);
```

## 3. 网关白名单

`config/xiaogj-youli-platform-gateway.yml` 的 `whiteUrls` 已加入：

- `/api/crm/sso/youli/register`
- `/api/crm/sso/youli/redeem`
- `/api/crm/sso/youli/revoke`

`issue` 不放白名单，必须携带业务登录态。修改后同步到 Nacos 并重启网关。

## 4. Octop 环境变量

| 变量 | 示例 | 说明 |
| --- | --- | --- |
| OCTOP_YOULI_ENABLED | true | 开启 handoff |
| OCTOP_YOULI_BASE_URL | http://youli-crm:9903 | Octop 容器到 CRM 的内网地址 |
| OCTOP_YOULI_MDM_URL | http://youli-mdm:9902 | Octop 容器到 MDM 的内网地址 |
| OCTOP_YOULI_CLIENT_ID | octop | 与 CRM 应用登记一致 |
| OCTOP_YOULI_CLIENT_SECRET | <secret> | 与 secret_hash 对应 |
| OCTOP_YOULI_AUDIENCE | octop | 固定受众 |
| OCTOP_YOULI_CALLBACK_URL | https://8.163.15.131/aiwork/login/youli/complete | 回调页地址 |
| OCTOP_YOULI_BUSINESS_ENTRY_URL | https://8.163.15.131/octop-login | 经贝签发 code 的入口页 |
| OCTOP_YOULI_TOKEN_TTL | 172800 | 业务 token 剩余寿命上限 |
| OCTOP_YOULI_COOKIE_SECURE | true | HTTPS 环境保持 true |

Octop 必须与 `youli-crm`、`youli-mdm` 处于同一 Compose 网络，或使用可访问的内网地址。

## 5. 上线前检查

| 检查 | 命令 / 位置 |
| --- | --- |
| CRM 三张表存在 | `SHOW TABLES LIKE 'youli_%'` |
| 应用记录启用 | `SELECT client_id, audience, callback_uri, enabled FROM youli_oauth_client` |
| 网关白名单生效 | Nacos 配置回读 + 网关重启日志 |
| Octop 配置生效 | `GET /api/auth/youli/status` 返回 enabled=true |
| 专用终端 | Redis 中出现 `用户名-10-行业`，原 PC 槽位仍存在 |
| 回调 cookie | 浏览器仅持有 Host-only、HttpOnly、Secure、SameSite=Lax 的 `youli_sso_<request_id>` |

## 6. 回滚

1. Octop 设置 `OCTOP_YOULI_ENABLED=false` 并重启。
2. 网关移除三条白名单并重启。
3. 需要彻底回滚时执行 SQL 文件头部的 DROP 语句；会话表与绑定表可保留，不影响本地登录。

## 7. 前端静态资源

`/aiwork/` 由 mcn-edge-nginx 直接读取宿主机目录
`/root/mcn/docker/nginx-edge/www/aiwork`，不经过 Octop 容器。该目录当前是
“会火”品牌构建，**不要用本仓库默认前端构建覆盖**；默认构建的品牌、文案和
首页布局都与会火版本不同。仅在确认能复现会火品牌资源后再发布前端。

确需用本仓库构建时，必须带子路径，否则页面会请求根路径 `/assets/` 并全部 404：

```bash
cd dashboard && VITE_BASE_PATH=/aiwork/ npm run build
```

产物写入 `src/octop/dashboard`，同步到上述目录后刷新页面；发布前保留
`aiwork.bak.*` 目录用于回滚。

