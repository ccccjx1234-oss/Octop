"""Exercise shared directory imports with real SQLite persistence and mocked Feishu."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from octop.config import OctopConfig
from octop.infra.auth.sso.crypto import encrypt_secret
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.feishu_kb import shared_sources
from octop.infra.feishu_kb.client import FeishuError
from octop.infra.feishu_kb.directories import DIRECTORY_SCOPES, FeishuDirectoryClient
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
    assert set(query["scope"][0].split()) == set(DIRECTORY_SCOPES)
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
