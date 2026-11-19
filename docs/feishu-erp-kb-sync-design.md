# Feishu, Youli and private knowledge synchronization

## Status and branch base

ERP binding is enabled after a session-bound Feishu verification. Document synchronization supports owner-only, explicitly enabled semantic retrieval in isolated private index collections;
ordinary knowledge-base registration remains excluded. The working branch is
`feature/feishu-auth-erp-kb-sync`, based on the locally available `main`;
`origin/develop` was not available in this checkout. Nothing has been pushed.
No production system has been contacted or migrated.

## Implemented identity and delegation

Feishu login requires the `tenant_key` from user info. Its subject includes
region, app ID, tenant and the upstream user ID. Existing unscoped identities
are rejected for login, rather than guessed or silently claimed. Their original
Octop owner can explicitly bind Feishu again while authenticated; this proves
both control of the Octop account and the newly presented Feishu identity.
Already scoped identities cannot be silently replaced by a different tenant or
app. Users with legacy Feishu-only accounts need a separate working Octop login
method or an administrator-assisted recovery before they can rebind. An
administrator must verify the person and identify the original Octop account
out of band, then use Users → Reset password (or the permission-protected
`POST /api/users/{user_id}/reset-password`). This writes a `user.password_reset`
audit event. Transfer the password securely; the user signs into that original
account and uses Account security → link Feishu to complete a new scoped OAuth
flow. Reset MFA separately if necessary. No subject, email or name match alone
authorizes a merge. The callback reports `legacy_feishu_recovery` when it finds
an unscoped identity during an unauthenticated login.

Authenticated `GET` and `DELETE /api/auth/youli/binding` provide status and
unlink. `POST` is enabled and requires a fresh, single-use Feishu proof bound
to the current user and session before accepting ERP credentials. Existing
valid delegations remain available subject to live verification.

The binding implementation records delegation only and does not create an ERP
login identity or change the primary SSO identity. Existing conflicting ERP
identities and other users' bindings are rejected. ERP login and older ERP
sessions for a reserved binding are denied, including after unlink. Unlinking
either ERP or Feishu revokes all matching local company/employee sessions and
advances the binding generation. A revoked binding remains as a reservation,
reported as unbound, so ambiguous historical ERP mappings cannot reactivate
access. Old mappings are retained because their provenance cannot be proven;
they may include legitimate preexisting identities. Releasing reservations and
cleaning mappings requires an audited provenance-aware recovery process.

MCN connector calls and tests use live session credentials and fail closed when
online verification fails. Stored connector token copies are never used, but
remain encrypted at rest because their provenance and concurrent updates cannot
be established safely here; a separate guarded cleanup is still needed. Local
unlink does not revoke the upstream credential or an already in-flight request.

A normal Octop access token does not contain a Youli sid. A Dashboard turn can
resolve the owner's current delegation locally. The in-process MCN connector
checks turn user ownership, active session,
expiry and MDM before returning a bound credential; legacy Youli tools check
the same properties. Internal HTTP MCP lacks an authenticated turn user and
therefore does not receive the new bound delegation. Local revocation cannot
cancel an upstream request already in flight.

Migration `028_youli_identity_binding` adds the binding and generation tables in
SQLite and PostgreSQL. A separate version is required so databases already at
v27 do not skip the new schema. It has only been exercised against temporary
SQLite databases; PostgreSQL and upgrades on deployment-like databases remain
unverified.

## ERP OAuth verification and remaining release gate

Migration `030_feishu_erp_reauth` stores purpose-bound OAuth states and proofs
for SQLite and PostgreSQL. `POST /api/auth/youli/binding/reauth/start` starts
PKCE for a linked Feishu provider, and `/binding/reauth/callback` exchanges the
code and verifies the full region, app, tenant and subject. Both use the exact
current bearer session; state and proof are single-use and expire. Provider
configuration is fingerprinted, and unlink, identity or provider changes fence
the claim and final write. The SQL files describe fresh migrations; an existing
unreleased v30 database made before the fingerprint column needs an upgrade
before this code is usable.

A fresh OAuth identity verification establishes control of the matching Feishu
session, subject to upstream OAuth and PKCE enforcement. It does not establish
when the user authenticated or that a password was re-entered. The account
security UI opens the Feishu callback in a popup, clears callback parameters
from history, verifies the same bearer session and keeps the proof in memory.
It uses an explicit Feishu/Lark authorization origin allowlist. Credentials
are displayed only after a successful proof and are not persisted by the UI.

The public binding POST calls `_bind_verified`, which consumes proof before ERP
calls and passes its claimed identity and generation into the final transactional
check. It stores only a delegation.

Real SQLite proof and transaction tests use mocked Feishu HTTP. The bounded
executor-backed test run timed out in this environment, so it does not establish
ERP or full-route success. PostgreSQL concurrency, actual tenant configuration,
OAuth provider behavior, ERP/MDM integration and deployment-like upgrades remain
unverified. No production account or database was accessed.

## Known limitations and rollout prerequisites

Unlink revokes locally and erases the encrypted token. It does **not** call the
existing upstream `revoke` endpoint: that endpoint identifies an account and
terminal rather than a particular token, and an asynchronous revoke can cancel
a concurrent new login or another session. Safe upstream revocation needs an
upstream token-specific contract or coordinated serialization; until then a
previously issued token may remain valid upstream until expiration. ERP binding
uses the CRM delegation endpoint, which assigns a distinct terminal to each
attempt. The independent employee login uses terminal 10. Migration 036 records
explicit unlink for bindings retained as reservations. Credentials revoked by
MDM leave the binding available for unlink or reauthorization.
Older MCN connector installations could have saved a copy of an employee token
in their own encrypted credentials. New session-based installations no longer
copy it, and tool resolution no longer falls back to that stored snapshot, but
unlink does not remove pre-existing copies from connector storage. Those copies
need a separately reviewed cleanup; do not assume all historical copies have
been erased. This stricter resolution also changes manually configured MCN
connectors: their stored token is not used as a fallback for in-process calls.

The binding UI, in-process connector integration, PostgreSQL behavior, and
production Feishu/Youli configuration require isolated integration validation
before release. This security stage uses genuine runtime packages, temporary
SQLite, and mocked upstreams; executor-backed async tests could not complete in
the sandbox. These checks do not establish production readiness.
A configured Feishu app must return `tenant_key`; Youli requires its existing
client, callback, business entry and MDM configuration. No credentials are
stored in this document.

## Shared directory imports

Open a knowledge base and choose **Connect Feishu**. The base owner can authorize multiple Feishu accounts against an enabled Feishu application, then select a Wiki space, a Wiki subtree, or a Drive folder. The authorization callback remains `/feishu-kb/callback`. Directory browsing requires the app and user grants for Wiki, Drive metadata, and read-only Docx, plus offline access for scheduled refresh. These connections are independent of login identities and the private document-staging grants.

Review displays the documents and the current knowledge-base audience. Confirmation persists the reviewed document IDs and audience before scheduling work. Imported copies use the knowledge base's existing access and search controls. The first version imports Docx as text, skips unsupported entries and Wiki shortcuts, and keeps newly found documents pending unless the owner explicitly allows automatic inclusion. The owner can review again, pause, resume, or disconnect a connection. Disconnect hides that connection's imported copies and schedules their storage and index cleanup.

The shared worker polls eligible jobs every minute and uses a database lease per source. A successful job publishes a bounded snapshot in a single metadata transaction. Transient failures retain the previous snapshot. New databases and existing installations require migration 037. Shared imports are excluded from graph extraction, and moving an imported document into a different knowledge base requires a new source approval in that base. Private document staging continues to use its existing scheduler switch.

## Private document staging (local implementation)

Migration 029 stores OAuth state, encrypted user grants, explicit docx sources,
their digests and private staged text. It supports SQLite and PostgreSQL DDL.
Only the owning authenticated Octop user can access these sources; the admin
role has no bypass. Sources are initially disabled, capped at 100 per user, and
accept intervals from 15 minutes to 30 days. Text is capped at 2 MB per document.
Content never enters ordinary `KnowledgeService` bases, documents, graph, sharing, or existing knowledge-base endpoints. Migration 032 adds a per-source retrieval switch, off by default. A switched-on source is read live on every `search_knowledge` invocation, checked against the owner and linked Feishu grant, chunked and embedded using the existing configured embedding service, and stored in a separate owner/source collection. No embedding provider is selected automatically. A digest and embedding configuration version govern reindexing. Private matches never enter the reranker and use a non-navigable source identifier instead of ordinary KB citations. Disable, revoke, delete, unlink, read failure and a changed source fence results and trigger vector deletion; persistent mapping records permit cleanup retries on startup or later operations. Cross-store deletion is not atomic. Staged text and indexed chunks are plaintext at rest.

Explicit retrieval permission allows the configured embedding service to receive document text. A response already sent to a model or user cannot be withdrawn; tool results and chat history may retain previously disclosed content after access is revoked.

The API under `/api/feishu-kb` provides:

- `GET /status`: list only the current owner’s linked supported identities, tenant, requested scopes and local grant status, plus the scheduler configuration flag. Scopes are requested, not independently confirmed. No credentials are returned.
- `POST /authorize` with `provider_id`: return a PKCE URL, binding state to
  the current owner and exact Bearer token for five minutes.
- `POST /callback` with `state` and `code`: consume state once and verify the
  returned tenant, app and user against the owner's linked Feishu identity.
  The exact same Bearer token must be used. State is consumed even on failure.
- `POST /sources` with `provider_id`, `document_id`; `GET /sources` lists
  private status; `PUT /sources/{id}` sets `enabled`, `interval_seconds` and optional `retrieval_enabled`.
- `POST /sources/{id}/sync`; `GET /sources/{id}/content` fetches upstream
  again before returning staged text and uses `Cache-Control: no-store`.
- `DELETE /sources/{id}` and `DELETE /grants/{provider_id}` delete local data.
  Unlinking or replacing the Feishu identity also removes its grants, sources
  and pending states. Upstream OAuth revocation is not attempted.

The authorization redirect is the configured Feishu provider's
`dashboard_origin` plus `/feishu-kb/callback`. The knowledge page provides the private-source drawer and callback landing page.
The popup clears its query from browser history immediately and sends the callback
parameters only to its same-origin opener. The opener checks the popup source and
original Bearer session before posting the callback with that exact token. Before
posting, a changed session or closing the drawer ends the local flow and requires
retry. Once posted, finalization cannot be cancelled: the drawer stays open until
it resolves. A session change or navigation cannot stop the original authenticated
request from committing; the interface clears the old session's data and advises
checking authorization status when returning to that account. The callback uses
the captured token and is never retried under a renewed session. The app redacts
the callback query from Uvicorn access logs; reverse proxies and other upstream
access logs must separately redact or suppress the entire `/feishu-kb/callback`
query. Clearing browser history occurs after the request and does not protect server logs. The drawer lists linked
identities, requested scopes, scheduler configuration, and private source status;
it supports explicit docx IDs, configuration, single-source sync and confirmed
deletions and the independent retrieval opt-in. Feishu China
providers only are supported; Lark is rejected. The redirect URI must be
registered with the Feishu app. Token renewal before the callback is posted invalidates the pending flow, which must then be restarted.

The document read client retries transient failures at most three times; no
failure returns a stale staged copy. A sync replaces content only when its
SHA-256 changes and a database lease is still held. Disable, delete, revoke,
identity unlink and lease expiry fence in-flight workers. A refresh claim is
committed before contacting Feishu and is never retried after an uncertain
outcome; another process fails closed while refresh is in progress. A crash or
lost response during refresh requires a new authorization. Successful refresh
persists the rotated grant before further use. The system scanner processes at
most 20 due sources per run, with per-source database leases. It is disabled by
default and is only registered when `OCTOP_FEISHU_KB_SCHEDULER_ENABLED=1` at
startup. Do not set this for production: a cross-process upstream request-rate
limiter is not yet implemented, and both PostgreSQL and real tenant behavior
remain unverified. Manual API reads and syncs are possible without that setting.

## Validation and rollout prerequisites

Offline test cases cover state expiry/replay and session/owner binding, identity
mismatch, encrypted grants, refresh failure, disabled owners, changed and
unchanged documents, permission/deletion/transient failure, API owner
isolation, lease fencing, revocation and restart. New semantic retrieval cases
exercise the agent tool and real local index with fake embeddings and mocked
Feishu HTTP, plus ordinary API isolation, cleanup retry and empty-KB tool
visibility. These new cases have not yet been run in this implementation pass.
PostgreSQL and real upstream OAuth remain unverified. Later integration testing requires an isolated
test tenant, a separately configured Feishu app with approved document scopes
and redirect URI, and explicit authorization to use those systems. This is
not a production-readiness claim.

Private semantic retrieval is available only to the authenticated dashboard requester
and only for sources selected on that turn. IM, scheduled, autonomous, and resumed
turns without that request authority fail closed. Omitted or empty selections do not
select private sources. Rebuild and query are serialized per source across processes;
SQLite uses a separate lock file and PostgreSQL uses an advisory session lock.

The thread recipient boundary is still under review. In particular, IM session
switching and rebinding must reject protected dashboard threads atomically with
private selection; otherwise a later IM turn may read a protected history. Do not
enable private retrieval until that boundary and the two-user transport tests have
been verified. The thread protection marker contains no document content and has
no automatic reset or safe-deletion policy.

## Final local validation snapshot — 2026-11-18 (Asia/Shanghai)

This snapshot supersedes earlier statements above that the offline tests have not run.

- Final affected backend regression: **2071 passed, 5 skipped**. The skips are one pre-existing harness configuration fixture incompatibility and four PostgreSQL acceptance cases without a local PostgreSQL server.
- Dashboard full suite: **1891 passed, 11 failed**. The same eleven failures were reproduced against HEAD baseline source with the same installed dependencies; no unrelated frontend fixes are included. TypeScript `tsc -b` passed.
- PostgreSQL acceptance cases in `tests/integration/test_feishu_pg_acceptance.py` are committed as **unexecuted and not independently reviewed**. Normal package-index preparation failed with HTTP 500; no network or package-signature restriction was bypassed.
- The complete authenticated WebSocket-to-actual-peer-graph identity chain remains **unproven**. Existing transport/tool and real offline graph checks must not be described as that end-to-end test.
- Codex Terra/xhigh hit its usage limit before that remaining integration test and the new PostgreSQL test review could complete; no alternate model was used.
- No real Feishu/ERP/embedding integration, production migration, deployment or tenant validation has been performed. Feishu scheduling and private semantic retrieval remain disabled by default. A separate test app, registered callbacks and explicit account authorization are still required.

These results are local implementation evidence, not a production-readiness claim.

## Upstream upgrade and merge notes (Octop core modifications)

When pulling or rebasing against upstream Octop, watch these core touchpoints added on this branch:

1. **Database schema (`035_user_mobile_linking.{sql,pg.sql}`)**:
   - Adds nullable `users.mobile` and partial unique index `idx_users_mobile_unique` (`WHERE mobile IS NOT NULL`), bumping `_schema_version` to `35` (and `tests/unit/db/test_db_pool.py`).
   - If upstream also adds a `035_*` migration, renumber this pair to the next free version (`0NN_user_mobile_linking.{sql,pg.sql}`), update `UPDATE _schema_version SET version = NN`, and update `test_db_pool.py` accordingly.
2. **Core user & SSO classes (`UserRepo`, `UserManager`, `SsoService`)**:
   - Business logic for phone normalization and tenant allowlist checks lives in the isolated module `src/octop/infra/auth/sso/mobile_linking.py`.
   - `UserRepo` (`infra/db/repos/users.py`) only adds `UserRow.mobile`, optional `create(..., mobile=None)`, `get_by_mobile`, and `set_mobile_if_empty`.
   - The existing `auto_link_mobile` / `allowed_tenants` settings control duplicate detection. A match requires login to the original account and an explicit binding. ERP-enabled installations require explicit binding for any new Feishu identity.
3. **Feishu OAuth PKCE endpoint alignment**:
   - `FeishuAdapter.authorize_url`, `feishu_kb/client.py`, `FeishuKbPanel.tsx`, and `AccountSecurityDrawer.tsx` use `https://accounts.feishu.cn/oauth/v3/authorize` to match `https://accounts.feishu.cn/oauth/v3/token` strict PKCE validation (real Feishu rejects `/open-apis/authen/v1/authorize` + `/oauth/v3/token` with `PKCE code challenge failed.`).
