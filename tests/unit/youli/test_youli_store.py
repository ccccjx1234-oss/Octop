"""Storage tests for Youli handoff requests, sessions and agent bindings."""

from __future__ import annotations

import time

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path):
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    return build_shared_services(db=db, paths=paths, config=OctopConfig())


def test_login_request_consumes_once(services):
    repo = services.youli_login_request_repo
    repo.create(
        request_id="req-1",
        state_hash="state-hash",
        client_id="octop",
        audience="octop",
        callback_url="https://example.test/aiwork/callback",
        verifier_hash="verifier-hash",
        verifier_blob=b"encrypted",
        browser_hash="browser-hash",
        expires_at=int(time.time()) + 60,
    )
    row = repo.get("req-1")
    assert row is not None
    assert row.verifier_blob == b"encrypted"
    assert repo.consume("req-1") is True
    assert repo.consume("req-1") is False


def test_login_request_expired_cannot_be_consumed(services):
    repo = services.youli_login_request_repo
    repo.create(
        request_id="req-expired",
        state_hash="state",
        client_id="octop",
        audience="octop",
        callback_url="https://example.test/cb",
        verifier_hash="v",
        verifier_blob=None,
        browser_hash="b",
        expires_at=int(time.time()) - 1,
    )
    assert repo.consume("req-expired") is False


def test_session_lifecycle(services):
    repo = services.youli_session_repo
    now = int(time.time())
    uid = services.user_repo.create(
        username="youli-alice",
        password_hash=None,
        role="youli_employee",
    )
    row = repo.create(
        sid="sid-1",
        user_id=uid,
        account_user_id=11,
        account_username="alice",
        company_id=21,
        employee_id=31,
        industry=100,
        token_blob=b"secret",
        token_expires_at=now + 600,
        abs_expires_at=now + 300,
    )
    assert row.active
    assert repo.get_active("sid-1") is not None
    assert repo.revoke("sid-1") is True
    assert repo.revoke("sid-1") is False
    assert repo.get_active("sid-1") is None
    revoked = repo.get("sid-1")
    assert revoked is not None and revoked.status == "revoked"
    assert revoked.token_blob is None


def test_session_absolute_expiry(services):
    repo = services.youli_session_repo
    now = int(time.time())
    uid = services.user_repo.create(
        username="youli-bob",
        password_hash=None,
        role="youli_employee",
    )
    repo.create(
        sid="sid-old",
        user_id=uid,
        account_user_id=11,
        account_username="alice",
        company_id=21,
        employee_id=31,
        industry=None,
        token_blob=None,
        token_expires_at=now + 600,
        abs_expires_at=now - 1,
    )
    assert repo.get_active("sid-old") is None


def test_agent_binding_is_idempotent(services):
    repo = services.youli_agent_binding_repo
    uid = services.user_repo.create(
        username="youli-carol",
        password_hash=None,
        role="youli_employee",
    )
    first = repo.create(
        company_id=1,
        employee_id=2,
        published_expert_id=3,
        user_id=uid,
        agent_id="agent-a",
    )
    second = repo.create(
        company_id=1,
        employee_id=2,
        published_expert_id=3,
        user_id=uid,
        agent_id="agent-b",
    )
    assert first.agent_id == "agent-a"
    assert second.agent_id == "agent-a"
    assert repo.is_business_agent("agent-a") is True
    assert repo.is_business_agent("agent-b") is False


def test_business_template_whitelist(services):
    repo = services.youli_business_template_repo
    repo.add(101, "经营分析专家")
    repo.add(101, "重复")
    assert repo.is_business_template(101) is True
    assert repo.is_business_template(102) is False
    assert [row.published_expert_id for row in repo.list()] == [101]
