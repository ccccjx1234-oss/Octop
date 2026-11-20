"""JWT sid and session-revocation tests for the Youli handshake."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from octop.api.deps import maybe_sliding_renew_token, resolve_user_from_token
from octop.config import OctopConfig
from octop.infra.auth.tokens import PURPOSE_ACCESS, decode_token, sign_token
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.users.identity import Role, User
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def server(tmp_path):
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    services.secret_repo.get_or_create("jwt", lambda: b"test-secret-32-bytes-long-000000")
    return SimpleNamespace(services=services, user_manager=UserManager(services))


def _make_user(server) -> User:
    uid = server.services.user_repo.create(
        username="youli-dave",
        password_hash=None,
        role="youli_employee",
    )
    user = User(
        id=uid,
        username="youli-dave",
        role=Role.USER,
        display_name="Dave",
        permissions=[],
    )
    server.user_manager.register_cached_user(user)
    return user


def _make_session(server, user: User, *, abs_offset: int = 300) -> str:
    now = int(time.time())
    server.services.youli_session_repo.create(
        sid="sid-test",
        user_id=user.id,
        account_user_id=1001,
        account_username="dave",
        company_id=2001,
        employee_id=3001,
        industry=7,
        token_blob=None,
        token_expires_at=now + 600,
        abs_expires_at=now + abs_offset,
    )
    return "sid-test"


def test_concurrent_session_replacement_keeps_one_active(server):
    from concurrent.futures import ThreadPoolExecutor

    from octop.infra.db.repos.youli import YouliSessionRepo

    user = _make_user(server)
    provider = server.services.sso_repo.upsert_by_kind(
        "youli",
        enabled=True,
        display_name="Youli",
        issuer="youli",
        client_id="test",
        client_secret_enc=None,
        scopes="",
        dashboard_origin=None,
        extra={},
    )
    server.services.user_repo.upsert_sso_identity(
        user.id, provider_id=provider.id, subject="youli:2001:3001"
    )
    repo = server.services.youli_session_repo
    other_pool = SqlitePool(repo._db.path)  # noqa: SLF001
    other_repo = YouliSessionRepo(other_pool)
    now = int(time.time())

    def login(item):
        selected_repo, sid = item
        return selected_repo.create_replacing(
            sid=sid,
            user_id=user.id,
            account_user_id=1001,
            account_username="dave",
            company_id=2001,
            employee_id=3001,
            industry=7,
            token_blob=None,
            token_expires_at=now + 600,
            abs_expires_at=now + 600,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(login, ((repo, "sid-a"), (other_repo, "sid-b"))))
    active = repo.list_active_for_user(user.id)
    assert len(active) == 1
    assert active[0].sid in {"sid-a", "sid-b"}
    assert sum(len(replaced) for _, replaced in results) == 1
    other_pool.close()


def test_session_replacement_rejects_user_without_independent_mapping(server):
    user = _make_user(server)
    now = int(time.time())
    with pytest.raises(ValueError, match="no independent mapping"):
        server.services.youli_session_repo.create_replacing(
            sid="sid-unmapped",
            user_id=user.id,
            account_user_id=1001,
            account_username="dave",
            company_id=2001,
            employee_id=3001,
            industry=7,
            token_blob=None,
            token_expires_at=now + 600,
            abs_expires_at=now + 600,
        )
    assert server.services.youli_session_repo.list_active_for_user(user.id) == []


@pytest.mark.asyncio
async def test_revoked_socket_session_is_closed(server):
    from unittest.mock import AsyncMock

    from starlette.websockets import WebSocketState

    from octop.api.routers.chat.ws import _ws_session_active

    user = _make_user(server)
    sid = _make_session(server, user)
    socket = SimpleNamespace(application_state=WebSocketState.CONNECTED, close=AsyncMock())
    assert await _ws_session_active(socket, server, sid, user.id)
    server.services.youli_session_repo.revoke(sid)
    assert not await _ws_session_active(socket, server, sid, user.id)
    socket.close.assert_awaited_once_with(code=4001, reason="auth:session_revoked")


def test_sign_token_carries_sid():
    token = sign_token(
        b"secret",
        sub=1,
        uname="u",
        role="user",
        purpose=PURPOSE_ACCESS,
        ttl_seconds=60,
        sid="sid-1",
    )
    payload = decode_token(b"secret", token)
    assert payload["sid"] == "sid-1"


def test_revoked_session_rejects_token(server):
    user = _make_user(server)
    sid = _make_session(server, user)
    token = sign_token(
        server.services.secret_repo.get("jwt"),
        sub=user.id,
        uname=user.username,
        role=user.role,
        purpose=PURPOSE_ACCESS,
        ttl_seconds=60,
        sid=sid,
    )
    assert resolve_user_from_token(server, token).id == user.id
    server.services.youli_session_repo.revoke(sid)
    with pytest.raises(OctopError):
        resolve_user_from_token(server, token)


def test_sliding_renew_keeps_sid_and_absolute_expiry(server):
    user = _make_user(server)
    sid = _make_session(server, user, abs_offset=120)
    token = sign_token(
        server.services.secret_repo.get("jwt"),
        sub=user.id,
        uname=user.username,
        role=user.role,
        purpose=PURPOSE_ACCESS,
        ttl_seconds=5,
        sid=sid,
    )
    renewed = maybe_sliding_renew_token(server, token, user)
    assert renewed is not None
    payload = decode_token(server.services.secret_repo.get("jwt"), renewed)
    assert payload["sid"] == sid
    assert int(payload["exp"]) <= int(time.time()) + 120
