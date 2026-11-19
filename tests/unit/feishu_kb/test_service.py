"""Offline security and persistence tests against a temporary SQLite database."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from octop.infra.auth.sso.crypto import encrypt_secret
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.feishu_kb.client import FeishuClient, FeishuError
from octop.infra.feishu_kb.scheduler import install_feishu_kb_job
from octop.infra.feishu_kb.service import FeishuKbService

SUBJECT = 'feishu-v1:["feishu","app","tenant","person"]'


@pytest.fixture
def setup(tmp_path: Path):
    db = SqlitePool(tmp_path / "test.db")
    with db.connect() as conn:
        conn.executescript("""
        CREATE TABLE users(id INTEGER PRIMARY KEY, disabled INTEGER DEFAULT 0);
        CREATE TABLE sso_providers(id INTEGER PRIMARY KEY, kind TEXT, enabled INTEGER);
        CREATE TABLE user_sso_identities(user_id INTEGER, provider_id INTEGER, subject TEXT);
        CREATE TABLE secrets(k TEXT PRIMARY KEY, v BLOB, created_at INTEGER, rotated_at INTEGER);
        CREATE TABLE _schema_version(version INTEGER);
        INSERT INTO _schema_version VALUES(28);
        INSERT INTO users(id) VALUES(1),(2);
        INSERT INTO sso_providers VALUES(10,'feishu',1);
        """)
        conn.executescript(Path("src/octop/infra/db/migrations/029_feishu_kb.sql").read_text())
        conn.executescript(
            Path("src/octop/infra/db/migrations/032_feishu_private_retrieval.sql").read_text()
        )
        conn.executescript(
            Path("src/octop/infra/db/migrations/033_feishu_private_index_versions.sql").read_text()
        )
        conn.execute("INSERT INTO user_sso_identities VALUES(1,10,?)", (SUBJECT,))
    secrets = SecretRepo(db)
    provider = SimpleNamespace(
        id=10,
        kind="feishu",
        enabled=1,
        client_id="app",
        client_secret_enc=encrypt_secret(secrets, "app-secret"),
        dashboard_origin="https://local.example",
        extra={},
    )
    services = SimpleNamespace(
        db=db, secret_repo=secrets, sso_repo=SimpleNamespace(get_by_id=lambda _: provider)
    )
    upstream = {
        "subject": "person",
        "content": "first",
        "status": 200,
        "refresh_status": 200,
        "refresh_calls": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v3/token":
            payload = json.loads(request.content)
            if payload["grant_type"] == "refresh_token":
                upstream["refresh_calls"] += 1
                if upstream["refresh_status"] != 200:
                    return httpx.Response(upstream["refresh_status"])
            return httpx.Response(
                200,
                json={
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                },
            )
        if request.url.path.endswith("/user_info"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"tenant_key": "tenant", "union_id": upstream["subject"]}},
            )
        if upstream["status"] != 200:
            return httpx.Response(upstream["status"])
        return httpx.Response(200, json={"code": 0, "data": {"content": upstream["content"]}})

    def factory(_provider):
        return FeishuClient(
            httpx.Client(transport=httpx.MockTransport(handler)),
            client_id="app",
            client_secret="app-secret",
            sleep=lambda _: None,
        )

    clock = [1000]
    service = FeishuKbService(services, client_factory=factory, clock=lambda: clock[0])
    yield SimpleNamespace(
        db=db, service=service, upstream=upstream, clock=clock, services=services, factory=factory
    )
    db.close()


def start(s):
    return parse_qs(urlparse(s.service.authorize(1, 10, "session")).query)["state"][0]


def grant(s):
    state = start(s)
    s.service.callback(1, "session", state, "code")
    return state


def source(s):
    grant(s)
    source_id = s.service.create(1, 10, "doc1")
    s.service.configure(1, source_id, True, 900)
    return source_id


def test_state_owner_session_expiry_and_replay(setup):
    s = setup
    state = start(s)
    for user, session in [(2, "session"), (1, "other")]:
        with pytest.raises(FeishuError, match="invalid_state"):
            s.service.callback(user, session, state, "code")
    s.service.callback(1, "session", state, "code")
    with pytest.raises(FeishuError, match="invalid_state"):
        s.service.callback(1, "session", state, "code")
    expired = start(s)
    s.clock[0] += 300
    with pytest.raises(FeishuError, match="invalid_state"):
        s.service.callback(1, "session", expired, "code")


def test_identity_mismatch_and_encrypted_storage(setup):
    s = setup
    state = start(s)
    s.upstream["subject"] = "intruder"
    with pytest.raises(FeishuError, match="identity_mismatch"):
        s.service.callback(1, "session", state, "code")
    assert s.service.repo.grant(1, 10) is None
    s.upstream["subject"] = "person"
    grant(s)
    row = s.service.repo.grant(1, 10)
    assert b"access-secret" not in bytes(row["token_blob"])
    assert b"refresh-secret" not in bytes(row["token_blob"])


def test_sync_idempotent_readthrough_and_owner_isolation(setup):
    s = setup
    doc = source(s)
    assert s.service.sync(1, doc) == "updated"
    assert s.service.sync(1, doc) == "unchanged"
    s.upstream["content"] = "changed"
    assert s.service.sync(1, doc) == "updated"
    assert s.service.read(1, doc) == "changed"
    with pytest.raises(FeishuError):
        s.service.read(2, doc)
    assert not s.service.repo.sources(2)


@pytest.mark.parametrize("status", [403, 404, 503])
def test_permission_missing_and_transient_failure_clear_content(setup, status):
    s = setup
    doc = source(s)
    s.service.sync(1, doc)
    s.upstream["status"] = status
    with pytest.raises(FeishuError):
        s.service.read(1, doc)
    assert s.service.repo.source(doc, 1)["content"] is None


def test_refresh_failure_is_not_retried_and_requires_reauthorization(setup):
    s = setup
    doc = source(s)
    s.clock[0] += 3600
    s.upstream["refresh_status"] = 503
    for _ in range(2):
        with pytest.raises(FeishuError):
            s.service.read(1, doc)
    assert s.upstream["refresh_calls"] == 1
    assert s.service.repo.grant(1, 10)["refreshing"] == 1


def test_lease_disable_revoke_and_restart(setup):
    s = setup
    doc = source(s)
    assert s.service.repo.claim(doc, 1, "one", 1000)
    restarted = FeishuKbService(s.services, client_factory=s.factory, clock=lambda: s.clock[0])
    assert not restarted.repo.claim(doc, 1, "two", 1000)
    restarted.configure(1, doc, False, 900)
    assert not restarted.repo.finish(doc, 1, "one", 1001, "updated", "hash", "secret")
    with pytest.raises(FeishuError):
        restarted.read(1, doc)
    restarted.configure(1, doc, True, 900)
    restarted.repo.revoke(1, 10)
    assert restarted.repo.source(doc, 1) is None


def test_scheduler_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OCTOP_FEISHU_KB_SCHEDULER_ENABLED", raising=False)
    installed = []

    class Cron:
        def schedule_system_job(self, job_id, **kwargs):
            installed.append(job_id)

    install_feishu_kb_job(Cron(), server=object())
    assert installed == ["feishu-shared-kb-scan"]


def test_refresh_is_serialized_and_recovery_requires_new_grant(setup):
    s = setup
    doc = source(s)
    s.clock[0] += 3600
    row = s.service.repo.grant(1, 10)
    assert s.service.repo.claim_refresh(1, 10, bytes(row["token_blob"]))
    restarted = FeishuKbService(s.services, client_factory=s.factory, clock=lambda: s.clock[0])
    with pytest.raises(FeishuError, match="grant_unavailable"):
        restarted.read(1, doc)
    assert s.upstream["refresh_calls"] == 0
    grant(s)
    assert restarted.read(1, doc) == "first"
    s.clock[0] += 3600
    assert restarted.sync(1, doc) == "unchanged"
    assert s.upstream["refresh_calls"] == 1


def test_identity_unlink_and_disabled_owner_fail_closed(setup):
    s = setup
    doc = source(s)
    s.service.sync(1, doc)
    with s.db.transaction() as conn:
        conn.execute("UPDATE users SET disabled=1 WHERE id=1")
    with pytest.raises(FeishuError):
        s.service.read(1, doc)
    with s.db.transaction() as conn:
        conn.execute("UPDATE users SET disabled=0 WHERE id=1")
        conn.execute("DELETE FROM user_sso_identities WHERE user_id=1")
    with pytest.raises(FeishuError):
        s.service.read(1, doc)


@pytest.mark.asyncio
async def test_api_owner_and_live_content(setup, monkeypatch):
    import fastapi.routing
    from fastapi import FastAPI

    from octop.api.deps import current_user, get_server
    from octop.api.routers import feishu_kb
    from octop.infra.users.identity import User

    s = setup
    app = FastAPI()
    app.include_router(feishu_kb.router, prefix="/api/feishu-kb")
    owner = [1]

    async def get_user():
        return User(owner[0], "user", "admin", None)

    async def get_test_server():
        return SimpleNamespace(services=s.services)

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    app.dependency_overrides[current_user] = get_user
    app.dependency_overrides[get_server] = get_test_server
    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", inline)
    monkeypatch.setattr(feishu_kb, "FeishuKbService", lambda _: s.service)
    headers = {"Authorization": "Bearer session"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/feishu-kb/authorize", json={"provider_id": 10}, headers=headers
        )
        assert response.status_code == 200
        state = parse_qs(urlparse(response.json()["url"]).query)["state"][0]
        response = await client.post(
            "/api/feishu-kb/callback", json={"state": state, "code": "code"}, headers=headers
        )
        assert response.status_code == 200
        response = await client.post(
            "/api/feishu-kb/sources",
            json={"provider_id": 10, "document_id": "doc1"},
            headers=headers,
        )
        source_id = response.json()["source_id"]
        response = await client.get(f"/api/feishu-kb/sources/{source_id}/content", headers=headers)
        assert response.status_code == 400
        response = await client.put(
            f"/api/feishu-kb/sources/{source_id}",
            json={"enabled": True, "interval_seconds": 900},
            headers=headers,
        )
        assert response.status_code == 200
        response = await client.get(f"/api/feishu-kb/sources/{source_id}/content", headers=headers)
        assert response.json() == {"content": "first"}
        response = await client.get("/api/feishu-kb/sources", headers=headers)
        assert response.status_code == 200
        [item] = response.json()
        assert item["has_staged_content"] is True
        assert "digest" not in item and "content" not in item
        response = await client.put(
            f"/api/feishu-kb/sources/{source_id}",
            json={"enabled": False, "interval_seconds": 900},
            headers=headers,
        )
        assert response.status_code == 200
        response = await client.get("/api/feishu-kb/sources", headers=headers)
        assert response.json()[0]["has_staged_content"] is False
        owner[0] = 2  # An administrator does not bypass ownership.
        response = await client.get(f"/api/feishu-kb/sources/{source_id}/content", headers=headers)
        assert response.status_code == 400
        response = await client.get("/api/feishu-kb/sources", headers=headers)
        assert response.json() == []
    assert app.openapi()["paths"]["/api/feishu-kb/sources/{source_id}/content"]["get"]["summary"]


def test_revoke_invalidates_pending_callback_and_cross_pool_lease(setup, tmp_path):
    s = setup
    doc = source(s)
    pending = start(s)
    other_db = SqlitePool(tmp_path / "test.db")
    try:
        from octop.infra.db.repos.feishu_kb import FeishuKbRepo

        other = FeishuKbRepo(other_db)
        assert s.service.repo.claim(doc, 1, "worker-a", 1000)
        assert not other.claim(doc, 1, "worker-b", 1000)
        assert other.claim(doc, 1, "worker-b", 1301)
        assert not s.service.repo.finish(doc, 1, "worker-a", 1301, "updated", "hash", "old")
        other.revoke(1, 10)
        with pytest.raises(FeishuError, match="invalid_state"):
            s.service.callback(1, "session", pending, "code")
        assert s.service.repo.source(doc, 1) is None
    finally:
        other_db.close()


def test_provider_app_change_cannot_reuse_grant(setup):
    s = setup
    doc = source(s)
    s.services.sso_repo.get_by_id(10).client_id = "different-app"
    with pytest.raises(FeishuError, match="grant_unavailable"):
        s.service.read(1, doc)


@pytest.mark.asyncio
async def test_status_is_owner_scoped_and_sanitized(setup, monkeypatch):
    import fastapi.routing
    from fastapi import FastAPI

    from octop.api.deps import current_user, get_server
    from octop.api.routers import feishu_kb
    from octop.infra.users.identity import User

    s = setup
    grant(s)
    source_id = s.service.create(1, 10, "doc")
    with s.db.transaction() as conn:
        conn.execute(
            "UPDATE feishu_kb_source SET status=? WHERE source_id=?", ("secret error", source_id)
        )
    app = FastAPI()
    app.include_router(feishu_kb.router, prefix="/api/feishu-kb")
    owner = [1]
    app.dependency_overrides[current_user] = lambda: User(owner[0], "user", "admin", None)
    app.dependency_overrides[get_server] = lambda: SimpleNamespace(services=s.services)

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", inline)
    monkeypatch.setattr(feishu_kb, "FeishuKbService", lambda _: s.service)
    monkeypatch.delenv("OCTOP_FEISHU_KB_SCHEDULER_ENABLED", raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/feishu-kb/status")
        assert response.status_code == 200
        assert response.json()["scheduler_configured"] is False
        assert response.json()["identities"][0]["consent"] == "authorized"
        assert response.json()["identities"][0]["tenant"] == "tenant"
        assert "secret" not in response.text and "token" not in response.text
        sources_response = await client.get("/api/feishu-kb/sources")
        assert sources_response.json()[0]["status"] == "error"
        assert "secret" not in sources_response.text and "digest" not in sources_response.text
        owner[0] = 2
        assert (await client.get("/api/feishu-kb/status")).json()["identities"] == []
        for interval in (899, 2592001):
            response = await client.put(
                "/api/feishu-kb/sources/any", json={"enabled": True, "interval_seconds": interval}
            )
            assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_status", [403, 404, 503])
async def test_private_semantic_agent_tool_live_owner_and_reindex(
    setup, monkeypatch, tmp_path, upstream_status
):
    from langgraph.config import var_child_runnable_config

    from octop.infra.feishu_kb import private_index
    from octop.infra.knowledge import retrieve
    from octop.infra.knowledge.plane import open_knowledge_index
    from octop.infra.knowledge.private_authority import dashboard_authority, install, reset
    from octop.infra.knowledge.tools import build_knowledge_tools

    s = setup
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path / "home"))
    doc = source(s)
    assert not s.service.repo.source(doc, 1)["retrieval_enabled"]
    s.service.configure(1, doc, True, 900, True)
    settings = {"knowledge_embedding_model": "fake-v1"}
    s.services.settings_repo = SimpleNamespace(get=settings.get)
    s.services.provider_repo = SimpleNamespace(get=lambda _: None)
    s.services.knowledge_repo = SimpleNamespace(list_visible=lambda _: [], list_all=lambda: [])
    calls = []

    def embed(_services, texts):
        calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(private_index, "FeishuKbService", lambda _: s.service)
    monkeypatch.setattr(private_index, "assert_knowledge_usable", lambda *_: None)
    monkeypatch.setattr(private_index, "embed_knowledge_texts", embed)
    monkeypatch.setattr(retrieve, "assert_knowledge_usable", lambda *_: None)
    monkeypatch.setattr(retrieve, "embed_knowledge_texts", embed)
    monkeypatch.setattr(retrieve, "get_rerank_capability", lambda *_: {"usable": False})
    monkeypatch.setattr(retrieve, "graph_enabled", lambda *_: False)
    tool = build_knowledge_tools(s.services)[0]

    async def call(user=1, executor=1, admin=False):
        authority = install(dashboard_authority(executor, [private_index.PREFIX + doc]))
        token = var_child_runnable_config.set(
            {
                "configurable": {
                    "user": str(user),
                    "octop_user_id": executor,
                    "user_is_admin": admin,
                    "knowledge_base_ids": [private_index.PREFIX + doc],
                }
            }
        )
        try:
            return await tool.ainvoke({"query": "fact"})
        finally:
            var_child_runnable_config.reset(token)
            reset(authority)

    assert "first" in await call()
    with s.db.connect() as c:
        name = c.execute("SELECT collection_id FROM feishu_kb_private_index_versions WHERE source_id=?", (doc,)).fetchone()["collection_id"]
    index = open_knowledge_index(s.services, name)
    assert index.list_doc_chunks(doc) == [(0, "first")]
    assert "octop-kb-citations" not in await call()
    assert calls.count(["first"]) == 1
    assert "first" not in await call(user=2, executor=2, admin=True)
    assert "first" not in await call(user=1, executor=2)
    s.upstream["content"] = "changed private content"
    assert "changed private content" in await call()
    settings["knowledge_embedding_model"] = "fake-v2"
    assert "changed private content" in await call()
    assert calls.count(["changed private content"]) == 2
    s.upstream["status"] = upstream_status
    assert "changed private content" not in await call()
    assert index.list_doc_chunks(doc) == []
    s.upstream["status"] = 200
    assert "changed private content" in await call()
    s.service.repo.revoke(1, 10)
    private_index.cleanup(s.services)
    assert index.list_doc_chunks(doc) == []
    assert "changed private content" not in await call()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["opt_out", "disable", "delete", "revoke", "unlink"])
async def test_private_semantic_revoke_during_embedding_fails_closed(
    setup, monkeypatch, tmp_path, action
):
    from langgraph.config import var_child_runnable_config

    from octop.infra.feishu_kb import private_index
    from octop.infra.knowledge import retrieve
    from octop.infra.knowledge.plane import open_knowledge_index
    from octop.infra.knowledge.private_authority import dashboard_authority, install, reset
    from octop.infra.knowledge.tools import build_knowledge_tools

    s = setup
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path / "home"))
    doc = source(s)
    s.service.configure(1, doc, True, 900, True)
    s.services.settings_repo = SimpleNamespace(get=lambda _: None)
    s.services.provider_repo = SimpleNamespace(get=lambda _: None)
    s.services.knowledge_repo = SimpleNamespace(list_visible=lambda _: [], list_all=lambda: [])
    monkeypatch.setattr(private_index, "FeishuKbService", lambda _: s.service)
    monkeypatch.setattr(private_index, "assert_knowledge_usable", lambda *_: None)
    monkeypatch.setattr(retrieve, "assert_knowledge_usable", lambda *_: None)
    monkeypatch.setattr(
        retrieve, "embed_knowledge_texts", lambda _, texts: [[1.0, 0.0] for _ in texts]
    )
    monkeypatch.setattr(retrieve, "get_rerank_capability", lambda *_: {"usable": False})
    monkeypatch.setattr(retrieve, "graph_enabled", lambda *_: False)

    def revoke_during_embed(_services, texts):
        if action == "opt_out":
            s.service.configure(1, doc, True, 900, False)
        elif action == "disable":
            s.service.configure(1, doc, False, 900)
        elif action == "delete":
            s.service.repo.delete_source(doc, 1)
        elif action == "revoke":
            s.service.repo.revoke(1, 10)
        else:
            with s.db.transaction() as c:
                c.execute("DELETE FROM user_sso_identities WHERE user_id=1 AND provider_id=10")
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(private_index, "embed_knowledge_texts", revoke_during_embed)
    authority = install(dashboard_authority(1, [private_index.PREFIX + doc]))
    token = var_child_runnable_config.set(
        {
            "configurable": {
                "user": "1",
                "octop_user_id": 1,
                "knowledge_base_ids": [private_index.PREFIX + doc],
            }
        }
    )
    try:
        result = await build_knowledge_tools(s.services)[0].ainvoke({"query": "fact"})
    finally:
        var_child_runnable_config.reset(token)
        reset(authority)
    assert "first" not in result
    assert (
        open_knowledge_index(s.services, private_index.collection(1, doc)).list_doc_chunks(doc)
        == []
    )


def test_private_cleanup_retries_after_cross_store_failure(setup, monkeypatch, tmp_path):
    from octop.infra.feishu_kb import private_index
    from octop.infra.knowledge.plane import open_knowledge_index

    s = setup
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path / "home"))
    doc = source(s)
    s.service.configure(1, doc, True, 900, True)
    with s.db.transaction() as c:
        c.execute(
            "INSERT INTO feishu_kb_private_index(source_id,user_id,digest,config_version,dirty) VALUES (?,?,?, ?,0)",
            (doc, 1, "digest", "version"),
        )
    index = open_knowledge_index(s.services, private_index.collection(1, doc))
    index.replace_doc_chunks(doc, ["secret"], [[1.0]])
    s.service.repo.delete_source(doc, 1)
    delete = private_index._delete
    monkeypatch.setattr(
        private_index, "_delete", lambda *_: (_ for _ in ()).throw(OSError("offline"))
    )
    private_index.cleanup(s.services)
    assert index.list_doc_chunks(doc) == [(0, "secret")]
    monkeypatch.setattr(private_index, "_delete", delete)
    private_index.cleanup(s.services)
    assert index.list_doc_chunks(doc) == []


def test_private_rebuilds_are_serialized_across_writers(setup, monkeypatch, tmp_path):
    """A newer writer cannot publish while an older writer is paused in embedding."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from octop.infra.feishu_kb import private_index
    from octop.infra.knowledge.plane import open_knowledge_index

    s = setup
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path / "home"))
    doc = source(s)
    s.service.configure(1, doc, True, 900, True)
    settings = {
        "knowledge_bases_enabled": "true",
        "knowledge_embedding_backend": "remote",
        "knowledge_embedding_model": "test-embedding",
        "knowledge_embedding_provider_id": "1",
    }
    provider = SimpleNamespace(
        enabled=True, api_key="test", base_url="https://embedding.invalid", name="test",
        get_models=lambda: [{"id": "test-embedding"}],
    )
    s.services.settings_repo = SimpleNamespace(get=settings.get)
    s.services.provider_repo = SimpleNamespace(get=lambda _: provider)
    monkeypatch.setattr(FeishuKbService, "_client", lambda _self, p: s.factory(p))
    started, release, second_started = Event(), Event(), Event()

    def embed(_services, texts):
        if texts == ["first"]:
            started.set()
            assert release.wait(10)
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(private_index, "embed_knowledge_texts", embed)

    def search():
        return private_index.search(s.services, 1, doc, [1.0, 0.0], 4)

    def newer():
        second_started.set()
        return search()

    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(search)
        assert started.wait(10)
        s.upstream["content"] = "new content"
        new = pool.submit(newer)
        assert second_started.wait(10)
        assert not new.done()
        release.set()
        assert old.result(timeout=10) is not None
        result = new.result(timeout=10)
    assert result is not None
    assert [hit.text for hit in result[0]] == ["new content"]
    digest, generation, config_version = result[1]
    index = open_knowledge_index(s.services, private_index.collection(1, doc, digest, config_version, generation))
    assert index.list_doc_chunks(doc) == [(0, "new content")]


def test_private_versions_survive_lost_lock_and_old_writer(setup, monkeypatch, tmp_path):
    """A writer resumed after lock loss cannot overwrite or delete the newer index."""
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from threading import Event
    from octop.infra.feishu_kb import private_index
    from octop.infra.knowledge.plane import open_knowledge_index

    s = setup
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path / "home"))
    doc = source(s)
    s.service.configure(1, doc, True, 900, True)
    s.services.settings_repo = SimpleNamespace(get=lambda _: None)
    s.services.provider_repo = SimpleNamespace(get=lambda _: None)
    monkeypatch.setattr(FeishuKbService, "_client", lambda _self, p: s.factory(p))
    monkeypatch.setattr(private_index, "_source_lock", lambda *_: nullcontext())
    monkeypatch.setattr(private_index, "assert_knowledge_usable", lambda *_: None)
    monkeypatch.setattr(private_index, "embed_knowledge_texts", lambda _, texts: [[1.0, 0.0] for _ in texts])
    started, release = Event(), Event()
    original_open = private_index.open_knowledge_index

    def open_index(services, name):
        index = original_open(services, name)
        original_replace = index.replace_doc_chunks
        def replace(source_id, texts, vectors):
            if texts == ["first"]:
                started.set()
                assert release.wait(10)
            return original_replace(source_id, texts, vectors)
        index.replace_doc_chunks = replace
        return index

    monkeypatch.setattr(private_index, "open_knowledge_index", open_index)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(private_index.search, s.services, 1, doc, [1.0, 0.0], 4)
        assert started.wait(10)
        s.upstream["content"] = "new content"
        new = pool.submit(private_index.search, s.services, 1, doc, [1.0, 0.0], 4).result(timeout=10)
        assert new is not None
        release.set()
        assert old.result(timeout=10) is None
    digest, generation, config_version = new[1]
    name = private_index.collection(1, doc, digest, config_version, generation)
    assert open_knowledge_index(s.services, name).list_doc_chunks(doc) == [(0, "new content")]
    assert private_index.fence(s.services, 1, doc, new[1])
    with s.db.connect() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM feishu_kb_private_index_versions WHERE source_id=?", (doc,)).fetchone()["n"] == 2
