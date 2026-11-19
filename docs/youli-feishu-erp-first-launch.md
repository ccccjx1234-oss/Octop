# Octop × 经贝首次上线清单（飞书登录 / ERP 绑定 / Youli 交接）

本清单覆盖 Octop 与经贝首次上线所需的账号、密钥、应用、数据库和网络配置，以及上线验收、回滚和阻断项。

不包含 Octop 基础安装参数（管理员密码、端口、数据库连接、LLM Key）；这些沿用现有部署。

## 1. 上线前确认

| 检查项 | 必须满足 |
| --- | --- |
| Octop 代码 | 发布包包含当前工作区的飞书 ERP 绑定改动；当前 `main` 工作区仍有未提交改动，不能直接把 `main` 当发布包。 |
| huihuo-mcn 代码 | MDM 发布包包含 `InnerAuthController`、`InnerLoginResultDto` 和 `AuthUserServiceImpl.innerLoginByPhone / innerLoginByEmployee`；当前 `main` 工作区同样有未提交改动。 |
| CRM SQL | 使用包含 `DB/v3.8.0/02_xgj_youli_crm_youli_sso.sql` 的发布分支；当前 huihuo-mcn `main` 没有该文件，脚本在 `origin/feature/mcn-creator-integration`。 |
| 生产入口 | 已确定 HTTPS 生产域名，且 Octop 对外公开地址与反向代理配置一致。 |
| 管理员权限 | 配置 SSO 的账号具备 `sso_feishu` 权限；建议使用管理员账号完成首次配置。 |
| 网络连通 | Octop 容器可访问 `youli-crm:9903` 和 `youli-mdm:9902`；如走 HTTPS，证书链或私有 CA 已准备好。 |

## 2. 密钥与配置总表

### 2.1 Octop 环境变量

| 变量 | 必填 | 示例 | 说明 |
| --- | --- | --- | --- |
| `OCTOP_YOULI_ENABLED` | 是 | `true` | 开启 Youli 交接和飞书 ERP 手机号门禁；关闭时飞书按原逻辑登录。 |
| `OCTOP_YOULI_MDM_URL` | 是 | `http://youli-mdm:9902` | Octop 到 MDM 的内网地址，用于手机号/员工校验。 |
| `OCTOP_YOULI_INNER_API_KEY` | 是 | `<shared-key>` | 与 MDM 的 `innerApiKey` 完全一致，请求头为 `X-Inner-Api-Key`。 |
| `OCTOP_YOULI_BASE_URL` | handoff 是 | `http://youli-crm:9903` | CRM handoff 服务地址；只做飞书 ERP 绑定时可暂不配置。 |
| `OCTOP_YOULI_CLIENT_ID` | handoff 是 | `octop` | 与 CRM `youli_oauth_client.client_id` 一致。 |
| `OCTOP_YOULI_CLIENT_SECRET` | handoff 是 | `<secret>` | CRM 只保存它的 SHA-256 十六进制值。 |
| `OCTOP_YOULI_AUDIENCE` | handoff 是 | `octop` | 固定受众，与 CRM 登记值一致。 |
| `OCTOP_YOULI_CALLBACK_URL` | handoff 是 | `https://<生产域名>/aiwork/login/youli/complete` | 必须与 CRM `callback_uri` 完全一致。 |
| `OCTOP_YOULI_BUSINESS_ENTRY_URL` | handoff 是 | `https://<生产域名>/octop-login` | 经贝签发 code 的入口页。 |
| `OCTOP_YOULI_TOKEN_TTL` | 否 | `172800` | 业务 token 剩余寿命上限，单位秒。 |
| `OCTOP_YOULI_COOKIE_SECURE` | handoff 是 | `true` | 生产 HTTPS 必须为 true；仅本地 HTTP 调试可为 false。 |
| `OCTOP_YOULI_MCN_URL` | 否 | `http://youli-mcn:9909` | 只有启用 MCN 连接器时才需要。 |
| `OCTOP_YOULI_CA_FILE` | 否 | `<容器内路径>/youli-ca.pem` | MDM/CRM 使用私有 CA 时配置，路径必须在容器内可读。 |
| `OCTOP_YOULI_REVALIDATE_SECONDS` | 否 | `30` | 会话复核间隔，默认 30 秒。 |
| `OCTOP_ACCESS_TOKEN_TTL` | 否 | `86400` | Octop JWT 默认有效期；Youli 登录会取它与业务 token 剩余时间的较小值。 |

仓库自带的 `deploy/docker/docker-compose.yml` 的 `environment` 列表没有包含 `OCTOP_YOULI_*`。如果使用这份 compose，必须把变量补进 `environment`；也可以写入容器内 `/data/.octop/env`（宿主机 `~/.octop/env`），服务启动时会加载。修改后重启 Octop。

### 2.2 对端密钥与登记

| 用途 | 配置位置 | 名称 | 要求 |
| --- | --- | --- | --- |
| MDM 内部鉴权 | MDM 配置中心 / Nacos | `xiaogj.youli.basic.innerApiKey` | 与 `OCTOP_YOULI_INNER_API_KEY` 相同；生产禁止为空。 |
| CRM OAuth 应用 | CRM 库 `youli_oauth_client` | `client_id` / `secret_hash` / `audience` / `callback_uri` | `client_id=octop`；`secret_hash` 为 Octop client secret 的 SHA-256。 |
| CRM 回调白名单 | `youli-platform-gateway.yml` | `whiteUrls` | 放行 `/api/crm/sso/youli/register`、`redeem`、`revoke`；`issue` 和 inner 接口不放行。 |
| 飞书应用凭证 | 飞书开放平台 | App ID / App Secret | 企业自建应用；App Secret 只写入 Octop 管理后台。 |
| 飞书手机号权限 | 飞书开放平台 | `contact:user.phone:readonly` | 必须审批通过并发布版本；否则 ERP 手机号匹配拿不到 `mobile`。 |

### 2.3 Octop 管理后台 SSO

| 字段 | 填写值 | 说明 |
| --- | --- | --- |
| 启用 | 打开 | 关闭时登录页不显示飞书入口。 |
| 区域 | 飞书 / Lark | 按企业租户所在区域选择，选错会导致授权域名错误。 |
| 显示名称 | `飞书` | 登录按钮文案。 |
| App ID | `<飞书 App ID>` | 飞书开放平台应用凭证。 |
| App Secret | `<飞书 App Secret>` | 只写不回显；保存后显示“已配置”。 |
| 回调地址 | `https://<生产域名>/api/auth/oauth/callback` | 把后台页面显示的回调地址原样登记到飞书应用。 |
| Scopes | 留空 | `OCTOP_YOULI_ENABLED=true` 时，代码会自动追加 `contact:user.phone:readonly`。 |
| Dashboard Origin | 同源留空 | 仅当前端与 API 不同源时填写前端 HTTPS origin。 |

后台表单通常只显示区域、名称、App ID、App Secret 和启用开关；回调地址以页面提示为准。Scopes 和 Dashboard Origin 可通过 `PUT /api/auth/oauth/providers/feishu` 显式设置。

### 2.4 数据库与网络

| 位置 | 动作 | 验证 |
| --- | --- | --- |
| Octop 控制面 | 升级后重启 Octop 服务；启动时自动执行 `run_migrations` | 存在 `sso_pending_binds` 表和 `sso_login_states.youli_sid` 字段。 |
| CRM 库 | 执行 `02_xgj_youli_crm_youli_sso.sql`；脚本头部 `USE` 默认是 `xgj_youli_crm_sit`，生产执行前必须改成生产库名（通常为 `xgj_youli_crm`）。 | 存在 `youli_oauth_client` / `youli_oauth_request` / `youli_auth_code` 三张表。 |
| Octop 到 CRM | 允许访问 `youli-crm:9903` | `OCTOP_YOULI_BASE_URL` 可连通。 |
| Octop 到 MDM | 允许访问 `youli-mdm:9902` | `OCTOP_YOULI_MDM_URL` 可连通。 |
| 网关 | 不把 `/api/auth/inner/**` 加入白名单 | 该接口只允许 Octop 内网直连。 |

## 3. 配置步骤

### 3.1 飞书开放平台

1. 创建企业自建应用，记录 `App ID` 和 `App Secret`。
2. 在“安全设置 / 重定向 URL”添加 `https://<生产域名>/api/auth/oauth/callback`，必须与 Octop 后台显示值完全一致。
3. 申请并通过 `contact:user.phone:readonly` 权限；该权限用于从 `/open-apis/authen/v1/user_info` 读取员工手机号。
4. 把应用可用范围设置为全部目标员工，并发布应用版本；只配权限不发布，员工侧仍然无法登录。
5. 在 Octop 管理后台“用户 → 单点登录 → 飞书”填写凭证并保存，点击“测试连接”。

### 3.2 Octop 环境变量

生产完整集合（含 Youli handoff）如下；只做飞书 ERP 绑定时，可去掉 handoff 相关项。

```bash
OCTOP_YOULI_ENABLED=true
OCTOP_YOULI_MDM_URL=http://youli-mdm:9902
OCTOP_YOULI_INNER_API_KEY=<shared-key>
OCTOP_YOULI_BASE_URL=http://youli-crm:9903
OCTOP_YOULI_CLIENT_ID=octop
OCTOP_YOULI_CLIENT_SECRET=<secret>
OCTOP_YOULI_AUDIENCE=octop
OCTOP_YOULI_CALLBACK_URL=https://<生产域名>/aiwork/login/youli/complete
OCTOP_YOULI_BUSINESS_ENTRY_URL=https://<生产域名>/octop-login
OCTOP_YOULI_COOKIE_SECURE=true
```

写入容器的 `environment` 或 `/data/.octop/env` 后重启 Octop。若使用外部 PostgreSQL，确认迁移命令使用同一 `OCTOP_DATABASE_URL`。

### 3.3 Octop 管理后台 SSO

UI 路径：管理后台 → 用户 → 单点登录 → 飞书。

需要自动化时，也可以调用接口（管理员身份，需 `sso_feishu` 权限）：

```bash
curl -X PUT 'https://<生产域名>/api/auth/oauth/providers/feishu' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <admin access token>' \
  -d '{
    "enabled": true,
    "display_name": "飞书",
    "client_id": "<飞书 App ID>",
    "client_secret": "<飞书 App Secret>",
    "extra": {"region": "feishu"}
  }'
```

保存后调用 `POST /api/auth/oauth/providers/feishu/test` 验证 App 凭证；该接口只验证 App ID/Secret，不验证手机号 scope，scope 仍需用真实登录验证。

### 3.4 MDM 配置

1. 在 Nacos 或 `config/xiaogj-youli-mdm-service.yml` 设置 `xiaogj.youli.basic.innerApiKey`，值与 Octop 的 `OCTOP_YOULI_INNER_API_KEY` 完全一致。可用 `openssl rand -hex 32` 生成 64 位十六进制 key。
2. 确认发布包包含 `POST /api/auth/inner/loginByPhone` 和 `POST /api/auth/inner/loginByEmployee`。
3. 确认网关没有把 `/api/auth/inner/**` 加入 `whiteUrls`；只允许 Octop 所在内网访问 `youli-mdm:9902`。
4. 如果两侧 key 都留空，接口会跳过鉴权，仅适合隔离的本机/内网联调，生产禁止留空。

### 3.5 CRM 配置

1. 在 CRM 库执行 `DB/v3.8.0/02_xgj_youli_crm_youli_sso.sql`；该脚本当前只在 `origin/feature/mcn-creator-integration` 分支，huihuo-mcn `main` 还没有。脚本头部 `USE xgj_youli_crm_sit` 是 SIT 库，生产执行前必须改成生产库名。
2. 生成 Octop client secret，并计算 SHA-256：

```bash
secret=$(openssl rand -hex 32)
printf '%s' "$secret" | shasum -a 256
printf '%s' "$secret" | sha256sum
```

取命令输出的第一列 64 位十六进制哈希写入 `secret_hash`。

3. 插入 `youli_oauth_client` 记录，`callback_uri` 与 `OCTOP_YOULI_CALLBACK_URL` 完全一致：

```sql
INSERT INTO youli_oauth_client
  (client_id, secret_hash, audience, callback_uri, enabled)
VALUES
  ('octop', '<sha256-of-secret>', 'octop',
   'https://<生产域名>/aiwork/login/youli/complete', 1);
```

4. 在网关 `whiteUrls` 放行 `/api/crm/sso/youli/register`、`/api/crm/sso/youli/redeem`、`/api/crm/sso/youli/revoke`；`/api/crm/sso/youli/issue` 仍需业务登录态。
5. 把网关配置同步到 Nacos 并重启网关，回读配置确认生效。

### 3.6 Octop 数据库迁移

```bash
# 升级后重启 Octop；服务启动时会自动执行 run_migrations
docker compose restart octop
```

当前版本没有 `octop db migrate` 命令，旧部署文档里的这条命令不要再使用。迁移会新增 `sso_pending_binds` 表，并给 `sso_login_states` 增加 `youli_sid` 字段。已有 Youli 交接表不需要重建。

## 4. 验收清单

| 验收项 | 操作 | 期望 |
| --- | --- | --- |
| Youli 开关（启用 handoff 时） | `GET /api/auth/youli/status` | `enabled=true` |
| 飞书入口 | `GET /api/auth/oauth/status` | `providers` 中 `feishu.enabled=true` |
| 飞书凭证 | 后台“测试连接” | 返回 App credentials are valid |
| 手机号 scope | 发起真实飞书登录 | 授权页无 scope 报错，回调能拿到 `mobile` |
| ERP 命中 | 用已开通 ERP 且手机号一致的飞书账号登录 | 直接登录，用户绑定到对应 `company/employee`；上游返回 sid 时 JWT 应带 sid |
| ERP 未命中 | 用未登记手机号的飞书账号登录 | 返回 `erp_bind_required` 和 `bind_ticket`，不创建新用户 |
| ERP 密码绑定 | 在绑定弹窗输入 ERP 手机号和密码 | 绑定成功，产生 `auth.feishu_erp_bind` 审计记录 |
| 绑定票一次性 | 重复提交同一个 `bind_ticket` | 第二次拒绝，不产生重复绑定 |
| MDM 故障 | 临时阻断 Octop 到 MDM 的网络 | 飞书登录失败关闭，返回 `erp_unavailable`，不生成用户 |
| 内部接口鉴权 | 不带 `X-Inner-Api-Key` 调用 `loginByPhone` | 配置 key 后返回 401；带正确 key 返回 200 |
| 密钥不泄露 | 检查浏览器地址栏、网关日志、应用日志 | 无 `client_secret`、`innerApiKey`、App Secret 明文 |

数据库核对：

```sql
SELECT ticket, provider_kind, reason, expires_at, consumed_at
FROM sso_pending_binds ORDER BY created_at DESC LIMIT 10;

SELECT ts, actor, action, target
FROM audit_log WHERE action = 'auth.feishu_erp_bind'
ORDER BY ts DESC LIMIT 10;
```

## 5. 回滚与轮换

| 场景 | 操作 |
| --- | --- |
| 关闭飞书 ERP 门禁 | 设置 `OCTOP_YOULI_ENABLED=false` 并重启 Octop；飞书回到原登录逻辑，本地账号不受影响。 |
| 只关闭飞书入口 | 在管理后台把飞书 SSO 的“启用”关闭；已绑定关系保留。 |
| 轮换 MDM key | 先生成新 key，同时更新 MDM 与 Octop，滚动重启；确认后再删除旧 key。 |
| 轮换 CRM secret | 生成新 secret，更新 `youli_oauth_client.secret_hash` 和 `OCTOP_YOULI_CLIENT_SECRET`，重启 Octop。 |
| 回滚数据库 | 保留 `sso_pending_binds` 和 `youli_sid`，不影响旧登录；只有确认不再使用时才按 SQL 头部 DROP 语句清理。 |

## 6. 上线阻断项

以下任一项未确认，不建议上线：

- 飞书应用未通过 `contact:user.phone:readonly` 权限审批或未发布版本。
- ERP 手机号不能保证全局唯一；MDM 的 `getByPhone` 使用 `limit 1` 且没有 `company_id` 过滤，可能绑定到错误租户。
- 生产 MDM 的 `innerApiKey` 为空，且接口并非严格内网隔离。
- 存量纯飞书用户不接受强制 ERP 绑定；除“有密码的管理员”外，其余用户会被新门禁拦截。
- MDM 故障时，任意非管理员飞书用户都无法登录，需确认业务可接受该可用性口径。

## 7. 配置依据

- Octop 部署说明：`docs/youli-integration-deploy.md`
- Octop 配置解析：`src/octop/config.py`
- 飞书 ERP 门禁与绑定状态机：`src/octop/infra/auth/youli/feishu_gate.py`
- SSO 回调接入：`src/octop/infra/auth/sso/service.py`
- 飞书适配器与手机号 scope：`src/octop/infra/auth/sso/providers/feishu.py`
- MDM 内部接口：huihuo-mcn 的 `InnerAuthController` 与 `AuthUserServiceImpl`
- CRM 迁移脚本：huihuo-mcn `origin/feature/mcn-creator-integration` 的 `DB/v3.8.0/02_xgj_youli_crm_youli_sso.sql`
