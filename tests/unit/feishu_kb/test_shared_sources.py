"""Exercise shared directory imports with real SQLite persistence and mocked Feishu."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import ValidationError

from octop.api.routers.shared_feishu import ApproveRequest
from octop.config import OctopConfig
from octop.infra.auth.sso.crypto import encrypt_secret
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.feishu_kb import shared_sources
from octop.infra.feishu_kb.client import FeishuError
from octop.infra.feishu_kb.directories import FeishuDirectoryClient
from octop.infra.feishu_kb.shared_sources import Scope, SharedFeishuService
from octop.infra.knowledge import jobs, retrieve
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OCTOP_HOME", str(tmp_path))
    db = SqlitePool(tmp_path / "shared.db")
    run_migrations(db)
    services = SharedServices(PathLayout(tmp_path), OctopConfig(), RepoBundle.from_pool(db))
    owner = services.user_repo.create(
        username="owner", role="user", permissions=["knowledge_bases"]
    )
    reader = services.user_repo.create(username="reader", role="user")
    base = services.knowledge_repo.create_base(
        owner_user_id=owner, name="Team", embedding_model="test", shared=True
    )
    other = services.knowledge_repo.create_base(
        owner_user_id=owner, name="Other", embedding_model="test"
    )
    provider = services.sso_repo.upsert_by_kind(
        "feishu",
        enabled=True,
        display_name="Feishu",
        issuer="https://accounts.feishu.cn",
        client_id="app",
        client_secret_enc=encrypt_secret(services.secret_repo, "test-secret"),
        scopes="offline_access",
        dashboard_origin="https://local.example",
        extra={},
    )
    upstream = {
        "documents": ["doc-a"],
        "content": {"doc-a": "First", "doc-b": "Second"},
        "unavailable": False,
    }
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requests.append((path, str(request.url.query)))
        if path == "/oauth/v3/token":
            grant = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "access_token": grant["code"],
                    "refresh_token": "test-refresh",
                    "expires_in": 3600,
                },
            )
        if path.endswith("/user_info"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "tenant_key": "team",
                        "union_id": request.headers["Authorization"].split()[-1],
                    }
                },
            )
        if path.endswith("/files"):
            assert request.url.params["folder_token"] == "department"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "files": [
                            {"token": document, "name": f"Title {document}", "type": "docx"}
                            for document in upstream["documents"]
                        ]
                        + [{"token": "sheet-a", "name": "Sheet", "type": "sheet"}],
                        "has_more": False,
                    }
                },
            )
        if path.endswith("/raw_content"):
            if upstream["unavailable"]:
                return httpx.Response(503)
            return httpx.Response(
                200, json={"data": {"content": upstream["content"][path.split("/")[-2]]}}
            )
        raise AssertionError(path)

    def factory(_provider):
        return FeishuDirectoryClient(
            httpx.Client(transport=httpx.MockTransport(handler)),
            client_id="app",
            client_secret="test-secret",
            sleep=lambda _: None,
        )

    clock = [1000]
    svc = SharedFeishuService(services, client_factory=factory, clock=lambda: clock[0])
    monkeypatch.setattr(shared_sources, "assert_knowledge_usable", lambda *_: None)
    yield SimpleNamespace(
        svc=svc,
        services=services,
        owner=owner,
        reader=reader,
        base=base,
        other=other,
        upstream=upstream,
        clock=clock,
        requests=requests,
        provider_id=provider.id,
    )
    db.close()


def authorize(env, account: str) -> str:
    url = env.svc.authorize(env.owner, env.provider_id, "session", account)
    query = parse_qs(urlsplit(url).query)
    assert set(query["scope"][0].split()) == {
        "docx:document:readonly",
        "wiki:wiki:readonly",
        "drive:drive:readonly",
        "offline_access",
    }
    state = query["state"][0]
    with pytest.raises(FeishuError, match="invalid_state"):
        env.svc.callback(env.owner, "another-session", state, account)
    env.svc.callback(env.owner, "session", state, account)
    with pytest.raises(FeishuError, match="invalid_state"):
        env.svc.callback(env.owner, "session", state, account)
    return next(row["id"] for row in env.svc.connections(env.owner) if row["label"] == account)


def test_directory_import_approval_incremental_sync_and_disconnect(environment, monkeypatch):
    env = environment
    assert env.svc.providers_list() == [{"id": env.provider_id, "name": "Feishu"}]
    finance = authorize(env, "finance")
    operations = authorize(env, "operations")
    assert finance != operations
    assert env.svc.connections(env.reader) == []
    with pytest.raises(FeishuError, match="connection_unavailable"):
        env.svc.browse(env.reader, finance, "wiki_spaces", None, None, None)
    scope = Scope("drive_folder", "department", "Department")
    with pytest.raises(PermissionError):
        env.svc.preview(env.reader, env.base.id, finance, scope)
    approval = env.svc.preview(env.owner, env.base.id, finance, scope)
    assert approval["visibility"]["visibility"] == "all"
    assert approval["skipped"] == 1
    assert not any(path.endswith("/raw_content") for path, _ in env.requests)
    env.upstream["documents"].append("doc-b")
    with pytest.raises(FeishuError, match="review_changed"):
        env.svc.approve(
            env.owner,
            env.base.id,
            finance,
            scope,
            approval["fingerprint"],
            schedule="manual",
            auto_add=False,
        )
    env.upstream["documents"].pop()
    source = env.svc.approve(
        env.owner,
        env.base.id,
        finance,
        scope,
        approval["fingerprint"],
        schedule="manual",
        auto_add=False,
    )
    assert env.svc.knowledge.list_documents(env.base.id, actor_user_id=env.reader) == []
    [(kb_id, document_id)] = env.svc.scan()
    assert kb_id == env.base.id
    [document] = env.svc.knowledge.list_documents(env.base.id, actor_user_id=env.reader)
    assert document.id == document_id and document.feishu_source_id == source
    for module in (jobs, retrieve):
        monkeypatch.setattr(module, "assert_knowledge_usable", lambda *_: None)
        monkeypatch.setattr(
            module, "embed_knowledge_texts", lambda _, items: [[1.0, 0.0] for _ in items]
        )
    jobs.process_document(env.services, kb_id, document_id)

    def search():
        return retrieve._retrieve_context_sync(
            env.services,
            user_id=env.reader,
            is_admin=False,
            query="First",
            knowledge_base_ids=[kb_id],
            k=3,
            char_budget=500,
            locale="en",
            visible_bases=None,
        )

    assert "First" in search()
    assert (
        env.svc.knowledge.read_text_document(kb_id, document_id, actor_user_id=env.reader)["text"]
        == "First"
    )
    with pytest.raises(PermissionError):
        env.svc.knowledge.move_document(
            kb_id, document_id, target_folder="", target_kb_id=env.other.id, actor_user_id=env.owner
        )

    env.upstream["documents"].append("doc-b")
    assert env.svc.repo.set_active(kb_id, source, env.owner, True, env.clock[0])
    assert env.svc.scan() == []
    assert env.svc.repo.sources(kb_id)[0]["pending_count"] == 1
    env.upstream["unavailable"] = True
    assert env.svc.repo.set_active(kb_id, source, env.owner, True, env.clock[0])
    assert env.svc.scan() == []
    assert env.svc.repo.sources(kb_id)[0]["status"] == "error"
    assert env.services.knowledge_repo.get_document(document_id) is not None

    assert env.svc.repo.set_active(kb_id, source, env.owner, True, env.clock[0])
    claimed = env.svc.repo.claim(source, "worker", env.clock[0], env.clock[0] + 100)
    assert claimed is not None
    assert env.svc.repo.disconnect(env.owner, finance, env.clock[0])
    assert not env.svc.repo.publish(
        source=claimed,
        token="worker",
        now=env.clock[0],
        insert=[],
        keep_ids={document_id},
        pending=0,
        skipped=0,
        approved_document_ids="[]",
    )
    assert env.services.knowledge_repo.get_document(document_id) is None
    assert env.services.knowledge_repo.get_base(kb_id).doc_count == 0
    assert env.svc.repo.sources(kb_id)[0]["status"] == "disconnected"
    assert env.svc.repo.connection(env.owner, operations)["token_blob"] is not None
    assert not search()


def test_drive_root_finds_folders_in_the_complete_unpaginated_list():
    rows = [
        {"token": f"doc-{index}", "name": f"Document {index}", "type": "docx"}
        for index in range(168)
    ]
    rows[0]["name"] = ""
    rows.append({"token": "folder", "name": "山河日记", "type": "folder"})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/open-apis/drive/v1/files"
        assert request.url.params["page_size"] == "50"
        return httpx.Response(200, json={"data": {"files": rows, "has_more": False}})

    client = FeishuDirectoryClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        client_id="app",
        client_secret="test-secret",
    )
    try:
        root = client.drive_files("test-token", folders_only=True)
        assert root.next_page is None
        assert [item.title for item in root.items] == ["山河日记"]
        with pytest.raises(FeishuError, match="invalid_response"):
            client.drive_files("test-token", folder="folder")
    finally:
        client.close()


def test_wiki_discovery_follows_pages_and_children_without_following_shortcuts():
    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert request.url.path == "/open-apis/wiki/v2/spaces/space/nodes"
        if "parent_node_token" in params:
            assert params["parent_node_token"] == "branch"
            rows = [
                {"node_token": "leaf", "title": "Leaf", "obj_type": "docx", "obj_token": "second"}
            ]
        elif "page_token" in params:
            assert params["page_token"] == "next"
            rows = [
                {
                    "node_token": "alias",
                    "title": "Alias",
                    "obj_type": "docx",
                    "obj_token": "other",
                    "has_child": True,
                    "node_type": "shortcut",
                }
            ]
        else:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {
                                "node_token": "branch",
                                "title": "Branch",
                                "obj_type": "docx",
                                "obj_token": "first",
                                "has_child": True,
                            }
                        ],
                        "has_more": True,
                        "page_token": "next",
                    }
                },
            )
        return httpx.Response(200, json={"data": {"items": rows, "has_more": False}})

    client = FeishuDirectoryClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        client_id="app",
        client_secret="test-secret",
    )
    try:
        docs, skipped = client.discover("test-token", "wiki_space", "space", None, True)
        assert [(doc.id, doc.path) for doc in docs] == [("first", ()), ("second", ("Branch",))]
        assert skipped == 0
    finally:
        client.close()


def test_interval_schedule_pending_review_flags_and_validation(environment):
    env = environment
    account = authorize(env, "ops")
    scope = Scope("drive_folder", "department", "Department")

    initial = env.svc.preview(env.owner, env.base.id, account, scope)
    assert initial["existing_source"] is False
    assert initial["new_count"] == 0
    assert [doc["is_new"] for doc in initial["documents"]] == [False]

    for bad_interval in (None, 0, 43201, True):
        with pytest.raises(FeishuError, match="invalid_schedule"):
            env.svc.approve(
                env.owner,
                env.base.id,
                account,
                scope,
                initial["fingerprint"],
                schedule="interval",
                auto_add=False,
                interval_minutes=bad_interval,  # type: ignore[arg-type]
            )

    source_id = env.svc.approve(
        env.owner,
        env.base.id,
        account,
        scope,
        initial["fingerprint"],
        schedule="interval",
        auto_add=False,
        interval_minutes=15,
    )
    row = env.svc.repo.sources(env.base.id)[0]
    assert row["schedule"] == "interval"
    assert row["interval_minutes"] == 15

    assert len(env.svc.scan()) == 1
    with env.services.db.connect() as conn:
        db_row = conn.execute(
            "SELECT schedule, interval_minutes, next_sync, pending_count FROM kb_feishu_sources WHERE id=?",
            (source_id,),
        ).fetchone()
    assert db_row["schedule"] == "daily"
    assert db_row["interval_minutes"] == 15
    assert db_row["next_sync"] == env.clock[0] + 15 * 60
    assert db_row["pending_count"] == 0

    # New document shows up as pending until approved, and preview marks it as is_new and puts it first.
    env.upstream["documents"].append("doc-b")
    env.clock[0] += 15 * 60
    assert env.svc.scan() == []
    assert env.svc.repo.sources(env.base.id)[0]["pending_count"] == 1

    review = env.svc.preview(env.owner, env.base.id, account, scope)
    assert review["existing_source"] is True
    assert review["new_count"] == 1
    assert [(doc["id"], doc["is_new"]) for doc in review["documents"]] == [
        ("doc-b", True),
        ("doc-a", False),
    ]

    # Re-approving immediately clears pending_count and accepts a 1-minute interval.
    env.svc.approve(
        env.owner,
        env.base.id,
        account,
        scope,
        review["fingerprint"],
        schedule="interval",
        auto_add=True,
        interval_minutes=1,
    )
    assert env.svc.repo.sources(env.base.id)[0]["pending_count"] == 0
    assert env.svc.repo.sources(env.base.id)[0]["interval_minutes"] == 1

    # Short 1-minute interval backs off to at least 300s on sync error.
    env.upstream["unavailable"] = True
    assert env.svc.scan() == []
    with env.services.db.connect() as conn:
        err_row = conn.execute(
            "SELECT status, next_sync FROM kb_feishu_sources WHERE id=?",
            (source_id,),
        ).fetchone()
    assert err_row["status"] == "error"
    assert err_row["next_sync"] == env.clock[0] + 300

    # Switching from interval to daily and manual clears interval_minutes and updates next_sync.
    env.upstream["unavailable"] = False
    daily_preview = env.svc.preview(env.owner, env.base.id, account, scope)
    env.svc.approve(
        env.owner,
        env.base.id,
        account,
        scope,
        daily_preview["fingerprint"],
        schedule="daily",
        auto_add=False,
    )
    daily_source = env.svc.repo.sources(env.base.id)[0]
    assert daily_source["schedule"] == "daily"
    assert daily_source["interval_minutes"] is None
    assert len(env.svc.scan()) == 1
    with env.services.db.connect() as conn:
        daily_next = conn.execute(
            "SELECT next_sync FROM kb_feishu_sources WHERE id=?", (source_id,)
        ).fetchone()["next_sync"]
    assert daily_next == env.clock[0] + 86400

    manual_preview = env.svc.preview(env.owner, env.base.id, account, scope)
    env.svc.approve(
        env.owner,
        env.base.id,
        account,
        scope,
        manual_preview["fingerprint"],
        schedule="manual",
        auto_add=False,
    )
    manual_source = env.svc.repo.sources(env.base.id)[0]
    assert manual_source["schedule"] == "manual"
    assert manual_source["interval_minutes"] is None
    assert env.svc.scan() == []
    with env.services.db.connect() as conn:
        manual_next = conn.execute(
            "SELECT next_sync FROM kb_feishu_sources WHERE id=?", (source_id,)
        ).fetchone()["next_sync"]
    assert manual_next == 0


def test_approve_request_interval_validation():
    base_kwargs = {
        "connection_id": "conn1",
        "kind": "drive_folder",
        "root": "folder1",
        "title": "Folder",
        "fingerprint": "a" * 64,
        "confirmed": True,
    }
    valid = ApproveRequest(**base_kwargs, schedule="interval", interval_minutes=15)
    assert valid.interval_minutes == 15

    with pytest.raises(ValidationError):
        ApproveRequest(**base_kwargs, schedule="interval")

    with pytest.raises(ValidationError):
        ApproveRequest(**base_kwargs, schedule="interval", interval_minutes=0)

    with pytest.raises(ValidationError):
        ApproveRequest(**base_kwargs, schedule="interval", interval_minutes=43201)


def test_transient_refresh_failure_releases_lock_while_permanent_error_keeps_locked(
    environment,
) -> None:
    env = environment
    account = authorize(env, "finance")
    scope = Scope("drive_folder", "department", "Department")
    approval = env.svc.preview(env.owner, env.base.id, account, scope)
    source = env.svc.approve(
        env.owner,
        env.base.id,
        account,
        scope,
        approval["fingerprint"],
        schedule="manual",
        auto_add=False,
    )
    # Advance clock past token expiry (3600s) to force refresh on next sync.
    env.clock[0] += 3600

    refresh_mode = {"mode": "timeout"}

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v3/token":
            mode = refresh_mode["mode"]
            if mode == "timeout":
                raise httpx.ReadTimeout("network blip", request=request)
            if mode == "rate_limit":
                return httpx.Response(200, json={"code": 99991400, "msg": "rate limit"})
            if mode == "bad_grant":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={"access_token": "fresh", "refresh_token": "r2", "expires_in": 3600},
            )
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "files": [{"token": "doc-a", "name": "Title doc-a", "type": "docx"}],
                        "has_more": False,
                    }
                },
            )
        if request.url.path.endswith("/raw_content"):
            return httpx.Response(200, json={"data": {"content": "Refreshed"}})
        raise AssertionError(request.url.path)

    env.svc.client_factory = lambda _p: FeishuDirectoryClient(
        httpx.Client(transport=httpx.MockTransport(flaky_handler)),
        client_id="app",
        client_secret="test-secret",
        sleep=lambda _: None,
    )

    assert env.svc.scan() == []
    assert env.svc.repo.sources(env.base.id)[0]["status"] == "error"
    assert env.svc.repo.connection(env.owner, account)["refreshing"] == 0

    refresh_mode["mode"] = "rate_limit"
    assert env.svc.repo.set_active(env.base.id, source, env.owner, True, env.clock[0])
    assert env.svc.scan() == []
    assert env.svc.repo.connection(env.owner, account)["refreshing"] == 0

    refresh_mode["mode"] = "ok"
    assert env.svc.repo.set_active(env.base.id, source, env.owner, True, env.clock[0])
    assert len(env.svc.scan()) == 1
    assert env.svc.repo.connection(env.owner, account)["refreshing"] == 0

    # Permanent 400 error keeps refreshing=1 (requires explicit reauthorization).
    env.clock[0] += 3600
    refresh_mode["mode"] = "bad_grant"
    assert env.svc.repo.set_active(env.base.id, source, env.owner, True, env.clock[0])
    assert env.svc.scan() == []
    assert env.svc.repo.connection(env.owner, account)["refreshing"] == 1


def test_nested_feishu_documents_preserve_folders_enter_graph_and_cleanup(
    environment, monkeypatch
) -> None:
    from octop.api.routers import shared_feishu as router_mod
    from octop.infra.feishu_kb.directories import DirectoryDocument
    from octop.infra.knowledge.graph_store import (
        ChunkEntity,
        ChunkGraph,
        ChunkRelation,
        SqliteKnowledgeGraphStore,
    )
    from octop.infra.users.identity import User

    env = environment
    account = authorize(env, "wiki")

    def fake_discover(self, access_token, kind, root, space, include_children):
        return [DirectoryDocument("doc-a", "Spec", ("Dept", "R&D"))], 0

    monkeypatch.setattr(FeishuDirectoryClient, "discover", fake_discover)
    scope = Scope("wiki_space", "space-1", "TeamWiki")
    approval = env.svc.preview(env.owner, env.base.id, account, scope)
    env.svc.approve(
        env.owner,
        env.base.id,
        account,
        scope,
        approval["fingerprint"],
        schedule="manual",
        auto_add=False,
    )

    [(kb_id, doc_id)] = env.svc.scan()
    docs = env.svc.knowledge.list_documents(kb_id, actor_user_id=env.owner)
    assert any(d.is_dir and d.path == "Dept" for d in docs)
    assert any(d.is_dir and d.path == "Dept/R&D" for d in docs)
    file_doc = next(d for d in docs if not d.is_dir)
    assert file_doc.id == doc_id
    assert file_doc.path.startswith("Dept/R&D/")

    for module in (jobs, retrieve):
        monkeypatch.setattr(module, "assert_knowledge_usable", lambda *_: None)
        monkeypatch.setattr(
            module, "embed_knowledge_texts", lambda _, items: [[1.0, 0.0] for _ in items]
        )
    jobs.process_document(env.services, kb_id, doc_id)
    assert doc_id in env.svc.knowledge._graph_document_ids(kb_id)

    local_doc = env.services.knowledge_repo.create_document(
        kb_id=kb_id, filename="local.md", content_type="text/markdown", byte_size=5, status="ready"
    )
    env.services.settings_repo.set("knowledge_graph_enabled", "true")
    SqliteKnowledgeGraphStore(kb_id).replace_document(
        doc_id,
        [
            ChunkGraph(
                0,
                (ChunkEntity("Dept"), ChunkEntity("Local")),
                (ChunkRelation("Dept", "Local", "uses", "Secret Feishu fact", 9),),
            )
        ],
    )
    assert SqliteKnowledgeGraphStore(kb_id).counts() == (2, 1)

    context_before = retrieve._retrieve_context_sync(
        env.services,
        user_id=env.owner,
        is_admin=False,
        query="Dept",
        knowledge_base_ids=[kb_id],
        k=3,
        char_budget=2000,
        locale="en",
        visible_bases=None,
    )
    assert "Secret Feishu fact" in context_before

    # Even if cleanup has not run after disconnect, graph facts must not leak.
    assert env.svc.repo.disconnect(env.owner, account, env.clock[0])
    assert local_doc.id is not None
    context_after_disconnect = retrieve._retrieve_context_sync(
        env.services,
        user_id=env.owner,
        is_admin=False,
        query="Dept",
        knowledge_base_ids=[kb_id],
        k=3,
        char_budget=2000,
        locale="en",
        visible_bases=None,
    )
    assert "Secret Feishu fact" not in context_after_disconnect

    env.svc._cleanup()
    assert SqliteKnowledgeGraphStore(kb_id).counts() == (0, 0)
    assert SqliteKnowledgeGraphStore(kb_id).document_states() == {}

    # Router disconnect also removes graph rows synchronously without waiting for cron.
    account2 = authorize(env, "wiki-2")
    approval2 = env.svc.preview(env.owner, env.base.id, account2, scope)
    env.svc.approve(
        env.owner,
        env.base.id,
        account2,
        scope,
        approval2["fingerprint"],
        schedule="manual",
        auto_add=False,
    )
    [(kb_id2, doc_id2)] = env.svc.scan()
    SqliteKnowledgeGraphStore(kb_id2).replace_document(
        doc_id2,
        [
            ChunkGraph(
                0,
                (ChunkEntity("Dept"), ChunkEntity("Local")),
                (ChunkRelation("Dept", "Local", "uses", "Secret Feishu fact", 9),),
            )
        ],
    )
    assert SqliteKnowledgeGraphStore(kb_id2).counts() == (2, 1)
    assert router_mod.disconnect(account2, user=User(env.owner, "owner", "user", None), svc=env.svc) == {
        "ok": True
    }
    assert SqliteKnowledgeGraphStore(kb_id2).counts() == (0, 0)
    assert SqliteKnowledgeGraphStore(kb_id2).document_states() == {}


def test_admin_can_list_and_pause_another_owners_shared_feishu_source(
    environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from octop.api.routers import shared_feishu as router_mod
    from octop.infra.users.identity import User

    env = environment
    account = authorize(env, "ops")
    scope = Scope("drive_folder", "department", "Department")
    approval = env.svc.preview(env.owner, env.base.id, account, scope)
    source_id = env.svc.approve(
        env.owner,
        env.base.id,
        account,
        scope,
        approval["fingerprint"],
        schedule="daily",
        auto_add=False,
    )

    admin = User(999, "admin", "admin", None)
    owner_user = User(env.owner, "owner", "user", None)
    non_owner = User(env.reader, "reader", "user", None)

    with pytest.raises(HTTPException) as denied:
        router_mod.sources(env.base.id, user=non_owner, svc=env.svc)
    assert denied.value.status_code == 403

    rows = router_mod.sources(env.base.id, user=admin, svc=env.svc)
    assert len(rows) == 1
    assert rows[0]["id"] == source_id

    res = router_mod.update_source(
        env.base.id,
        source_id,
        router_mod.SourceUpdate(active=False),
        user=admin,
        svc=env.svc,
    )
    assert res == {"ok": True}
    assert env.svc.repo.sources(env.base.id)[0]["active"] == 0

    # Admin must not reactivate another user's paused personal Feishu OAuth grant.
    with pytest.raises(HTTPException) as resume_denied:
        router_mod.update_source(
            env.base.id,
            source_id,
            router_mod.SourceUpdate(active=True),
            user=admin,
            svc=env.svc,
        )
    assert resume_denied.value.status_code == 404
    assert env.svc.repo.sources(env.base.id)[0]["active"] == 0

    # Connection owner can resume their own source.
    assert router_mod.update_source(
        env.base.id,
        source_id,
        router_mod.SourceUpdate(active=True),
        user=owner_user,
        svc=env.svc,
    ) == {"ok": True}
    assert env.svc.repo.sources(env.base.id)[0]["active"] == 1

    owner_rows = router_mod.sources(env.base.id, user=owner_user, svc=env.svc)
    assert len(owner_rows) == 1
    assert owner_rows[0]["owner_user_id"] == env.owner

    account_cleanup_fail = authorize(env, "ops-cleanup-fail")

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(env.svc, "_cleanup", fail_cleanup)
    assert router_mod.disconnect(account_cleanup_fail, user=owner_user, svc=env.svc) == {"ok": True}
