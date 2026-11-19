"""Tests for mobile normalization and tenant-scoped SSO auto-linking."""

from __future__ import annotations

import json

import pytest

from octop.config import OctopConfig
from octop.infra.auth.sso.crypto import encrypt_secret
from octop.infra.auth.sso.mobile_linking import (
    normalize_mobile,
    parse_allowed_tenants,
    should_auto_link_mobile,
)
from octop.infra.auth.sso.service import SsoService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout


def test_normalize_mobile_variants() -> None:
    assert normalize_mobile("+86 138-0013-8000") == "13800138000"
    assert normalize_mobile("008613800138000") == "13800138000"
    assert normalize_mobile("13800138000") == "13800138000"
    assert normalize_mobile("+1 (415) 555-0199") == "+14155550199"
    assert normalize_mobile("") is None
    assert normalize_mobile("abc") is None


def test_should_auto_link_mobile_requires_allowlisted_feishu_tenant() -> None:
    subject = "feishu-v1:" + json.dumps(["feishu", "cli_1", "tenant_corp", "ou_1"])
    assert should_auto_link_mobile("feishu", {"auto_link_mobile": False}, subject) is False
    assert (
        should_auto_link_mobile(
            "feishu",
            {"auto_link_mobile": True, "allowed_tenants": []},
            subject,
        )
        is False
    )
    assert (
        should_auto_link_mobile(
            "feishu",
            {"auto_link_mobile": True, "allowed_tenants": ["other_tenant"]},
            subject,
        )
        is False
    )
    assert (
        should_auto_link_mobile(
            "feishu",
            {"auto_link_mobile": True, "allowed_tenants": ["tenant_corp"]},
            subject,
        )
        is True
    )
    assert parse_allowed_tenants("t1, t2\nt1 ; t3") == ["t1", "t2", "t3"]
    assert should_auto_link_mobile("dingtalk", {"auto_link_mobile": True}, "dt_uid") is True


@pytest.mark.asyncio
async def test_dingtalk_then_feishu_requires_authenticated_binding_for_mobile_match(tmp_path) -> None:
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    users = UserManager(services)
    sso = SsoService(services, users)

    dt = services.sso_repo.upsert_by_kind(
        "dingtalk",
        enabled=True,
        display_name="DingTalk",
        issuer="",
        client_id="dt_app",
        client_secret_enc=encrypt_secret(services.secret_repo, "dt_sec"),
        scopes="openid",
        dashboard_origin=None,
        extra={"auto_link_mobile": True},
    )
    fs = services.sso_repo.upsert_by_kind(
        "feishu",
        enabled=True,
        display_name="Feishu",
        issuer="",
        client_id="fs_app",
        client_secret_enc=encrypt_secret(services.secret_repo, "fs_sec"),
        scopes="",
        dashboard_origin=None,
        extra={"auto_link_mobile": True, "allowed_tenants": ["tenant_corp"]},
    )

    first = await users.resolve_or_create_sso_user(
        provider_id=dt.id,
        subject="dt_union_1",
        claims={"name": "Alice", "mobile": "13800138000"},
        allow_mobile_link=should_auto_link_mobile("dingtalk", dt.extra, "dt_union_1"),
    )

    fs_subject = "feishu-v1:" + json.dumps(["feishu", "fs_app", "tenant_corp", "ou_alice"])
    with pytest.raises(OctopError) as exc_info:
        await users.resolve_or_create_sso_user(
            provider_id=fs.id,
            subject=fs_subject,
            claims={"mobile": "+86 138-0013-8000"},
            allow_mobile_link=True,
        )
    assert exc_info.value.code is ErrorCode.SSO_BINDING_REQUIRED
    assert services.user_repo.get_by_sso(fs.id, fs_subject) is None

    second = await users.bind_sso_identity(
        user_id=first.id,
        provider_id=fs.id,
        subject=fs_subject,
        claims={"name": "Alice", "mobile": "+86 138-0013-8000"},
        allow_mobile_link=should_auto_link_mobile("feishu", fs.extra, fs_subject),
    )

    assert second.id == first.id
    identities = services.user_repo.list_sso_identities(first.id)
    assert {(item.provider_id, item.subject) for item in identities} == {
        (dt.id, "dt_union_1"),
        (fs.id, fs_subject),
    }

    # Untrusted Feishu tenant with the same mobile must NOT auto-link to Alice.
    untrusted_subject = "feishu-v1:" + json.dumps(
        ["feishu", "fs_app", "tenant_other", "ou_mallory"]
    )
    third = await users.resolve_or_create_sso_user(
        provider_id=fs.id,
        subject=untrusted_subject,
        claims={"name": "Mallory", "mobile": "+8613800138000"},
        allow_mobile_link=should_auto_link_mobile("feishu", fs.extra, untrusted_subject),
    )
    assert third.id != first.id

    # A different Feishu user claiming Alice's mobile in the same tenant cannot overwrite Alice's Feishu link.
    other_same_tenant = "feishu-v1:" + json.dumps(["feishu", "fs_app", "tenant_corp", "ou_bob"])
    with pytest.raises(OctopError) as exc_info:
        await users.resolve_or_create_sso_user(
            provider_id=fs.id,
            subject=other_same_tenant,
            claims={"name": "Bob", "mobile": "13800138000"},
            allow_mobile_link=should_auto_link_mobile("feishu", fs.extra, other_same_tenant),
        )
    assert exc_info.value.code is ErrorCode.SSO_BINDING_REQUIRED
    assert sso is not None

    await users.unbind_sso_identity(user_id=first.id, kind="feishu")
    with pytest.raises(OctopError) as exc_info:
        await users.resolve_or_create_sso_user(
            provider_id=fs.id,
            subject=fs_subject,
            claims={"mobile": "13800138000"},
            allow_mobile_link=True,
        )
    assert exc_info.value.code is ErrorCode.SSO_BINDING_REQUIRED
    assert services.user_repo.get_by_sso(fs.id, fs_subject) is None


@pytest.mark.asyncio
async def test_bind_sso_identity_backfills_mobile_for_duplicate_detection(tmp_path) -> None:
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    users = UserManager(services)

    local_uid = services.user_repo.create(
        username="local_bob",
        password_hash="hashed",
        role="user",
        display_name="Bob",
    )
    dt = services.sso_repo.upsert_by_kind(
        "dingtalk",
        enabled=True,
        display_name="DingTalk",
        issuer="",
        client_id="dt_app",
        client_secret_enc=encrypt_secret(services.secret_repo, "dt_sec"),
        scopes="openid",
        dashboard_origin=None,
        extra={"auto_link_mobile": True},
    )
    fs = services.sso_repo.upsert_by_kind(
        "feishu",
        enabled=True,
        display_name="Feishu",
        issuer="",
        client_id="fs_app",
        client_secret_enc=encrypt_secret(services.secret_repo, "fs_sec"),
        scopes="",
        dashboard_origin=None,
        extra={"auto_link_mobile": True, "allowed_tenants": ["tenant_corp"]},
    )

    await users.bind_sso_identity(
        user_id=local_uid,
        provider_id=dt.id,
        subject="dt_bob",
        claims={"name": "Bob", "mobile": "13900139000"},
        allow_mobile_link=True,
    )
    stored = services.user_repo.get(local_uid)
    assert stored is not None and stored.mobile == "13900139000"

    fs_subject = "feishu-v1:" + json.dumps(["feishu", "fs_app", "tenant_corp", "ou_bob"])
    with pytest.raises(OctopError) as exc_info:
        await users.resolve_or_create_sso_user(
            provider_id=fs.id,
            subject=fs_subject,
            claims={"name": "Bob", "mobile": "+86 139-0013-9000"},
            allow_mobile_link=should_auto_link_mobile("feishu", fs.extra, fs_subject),
        )
    assert exc_info.value.code is ErrorCode.SSO_BINDING_REQUIRED
    assert services.user_repo.get_by_sso(fs.id, fs_subject) is None


@pytest.mark.asyncio
async def test_concurrent_create_mobile_collision_requires_explicit_binding(
    tmp_path, monkeypatch
) -> None:
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    users = UserManager(services)

    dt = services.sso_repo.upsert_by_kind(
        "dingtalk",
        enabled=True,
        display_name="DingTalk",
        issuer="",
        client_id="dt_app",
        client_secret_enc=encrypt_secret(services.secret_repo, "dt_sec"),
        scopes="openid",
        dashboard_origin=None,
        extra={"auto_link_mobile": True},
    )
    fs = services.sso_repo.upsert_by_kind(
        "feishu",
        enabled=True,
        display_name="Feishu",
        issuer="",
        client_id="fs_app",
        client_secret_enc=encrypt_secret(services.secret_repo, "fs_sec"),
        scopes="",
        dashboard_origin=None,
        extra={"auto_link_mobile": True, "allowed_tenants": ["tenant_corp"]},
    )

    first_uid = services.user_repo.create(
        username="dt_first",
        password_hash=None,
        role="user",
        display_name="Carol",
        sso_provider_id=dt.id,
        sso_subject="dt_carol",
        mobile="13700137000",
    )
    services.user_repo.upsert_sso_identity(first_uid, provider_id=dt.id, subject="dt_carol")

    original_get_by_mobile = services.user_repo.get_by_mobile
    calls = {"count": 0}

    def stale_then_fresh(mobile: str):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return original_get_by_mobile(mobile)

    monkeypatch.setattr(services.user_repo, "get_by_mobile", stale_then_fresh)

    fs_subject = "feishu-v1:" + json.dumps(["feishu", "fs_app", "tenant_corp", "ou_carol"])
    with pytest.raises(OctopError) as exc_info:
        await users.resolve_or_create_sso_user(
            provider_id=fs.id,
            subject=fs_subject,
            claims={"name": "Carol", "mobile": "13700137000"},
            allow_mobile_link=True,
        )
    assert exc_info.value.code is ErrorCode.SSO_BINDING_REQUIRED
    assert services.user_repo.get_by_sso(fs.id, fs_subject) is None
    assert services.user_repo.count() == 1
