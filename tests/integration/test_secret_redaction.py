"""List and read endpoints must not return stored provider secrets."""

from __future__ import annotations

import json
from typing import Any

from tests.support.auth import create_user


async def test_provider_reads_hide_api_key_from_normal_users(env: Any) -> None:
    client, _srv, admin_auth = env
    secret = "sk-secret-value"
    created = await client.post(
        "/api/admin/providers",
        headers=admin_auth,
        json={"name": "openai", "kind": "openai", "api_key": secret},
    )
    assert created.status_code == 201
    created_body = created.json()
    assert "api_key" not in created_body
    assert created_body["api_key_set"] is True
    assert secret not in created.text
    assert created_body["api_key_preview"] != secret

    user_auth = await create_user(client, admin_auth, username="reader")
    listed = await client.get("/api/providers", headers=user_auth)
    assert listed.status_code == 200
    row = next(item for item in listed.json() if item["name"] == "openai")
    assert "api_key" not in row
    assert row["api_key_set"] is True
    assert secret not in listed.text

    admin_listed = await client.get("/api/admin/providers", headers=admin_auth)
    assert admin_listed.status_code == 200
    admin_row = next(item for item in admin_listed.json() if item["name"] == "openai")
    assert "api_key" not in admin_row
    assert secret not in admin_listed.text


async def test_voice_reads_hide_api_key_and_keep_stored_secret(env: Any) -> None:
    client, srv, admin_auth = env
    secret = "sk-voice-secret"
    created = await client.post(
        "/api/admin/voice/providers",
        headers=admin_auth,
        json={
            "name": "openai-stt",
            "kind": "openai",
            "capability": "stt",
            "api_key": secret,
            "extra_json": json.dumps({"secret_key": "super-secret", "region": "ap"}),
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert "api_key" not in body
    assert body["api_key_set"] is True
    assert body["extra"].get("secret_key") is None
    assert body["extra"]["secret_key_set"] is True
    assert body["extra"]["region"] == "ap"
    assert secret not in created.text
    assert "super-secret" not in created.text

    user_auth = await create_user(client, admin_auth, username="voice-reader")
    listed = await client.get("/api/voice/providers", headers=user_auth)
    assert listed.status_code == 200
    assert secret not in listed.text
    assert "super-secret" not in listed.text

    patched = await client.patch(
        f"/api/admin/voice/providers/{body['id']}",
        headers=admin_auth,
        json={"extra_json": json.dumps({"region": "ap", "secret_key_set": True})},
    )
    assert patched.status_code == 200
    stored = srv.services.voice_provider_repo.get(body["id"])
    assert stored is not None
    assert stored.get_extra()["secret_key"] == "super-secret"
    assert "super-secret" not in patched.text
