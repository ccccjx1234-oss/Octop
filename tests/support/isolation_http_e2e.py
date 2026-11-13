"""Authenticated HTTP journey for Linux user isolation and file sharing.

Run inside the disposable ``octop:local`` container. The test starts the real
OctopServer and ASGI app, uses real JWT authentication, and patches only the
LLM factory so the container needs no network. Agent construction, API routing,
per-user runtimes, low-privilege file workers, and memory stay real.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import uvicorn
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from octop.api.app import build_app
from octop.config import DatabaseConfig
from octop.infra.db.rebind import persist_database_config
from octop.infra.server import OctopServer
from octop.infra.setup.password_file import read_password
from octop.infra.users.permissions import BASELINE_PERMISSIONS

PASSWORD = "TestPass12"
SECRET = b"Alice private payload v1"
UPDATED_SECRET = b"Alice private payload v2"


class OfflineToolModel(FakeMessagesListChatModel):
    """Minimal harness chat model; construction must never reach the network."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> OfflineToolModel:
        return self

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if isinstance(messages[-1], ToolMessage):
            reply = AIMessage(content=messages[-1].content)
        else:
            reply = AIMessage(content="offline test response")
        return ChatResult(generations=[ChatGeneration(message=reply)])


def _offline_model() -> OfflineToolModel:
    return OfflineToolModel(responses=[AIMessage(content="unused")])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class IsolatedHttpJourney(unittest.TestCase):
    def test_authenticated_http_journey(self) -> None:
        with patch(
            "octop_harness.llm.factory.ChatModelFactory._build",
            side_effect=lambda *args, **kwargs: _offline_model(),
        ):
            asyncio.run(self._journey())

    async def _journey(self) -> None:
        with tempfile.TemporaryDirectory(prefix="octop-http-isolation-") as tmp:
            root = Path(tmp)
            # The data root's ancestors must be searchable by worker UIDs; keep
            # the data root itself private for Octop's own access checks.
            # This temporary parent is an ancestor of OCTOP_HOME. Worker UIDs
            # must be able to traverse it; IsolationPolicy tightens OCTOP_HOME.
            root.chmod(0o711)
            home = root / "octop-home"
            home.mkdir()
            os.environ["HOME"] = str(root / "user-home")
            os.environ["USERPROFILE"] = str(root / "user-home")
            os.environ["OCTOP_USER_ISOLATION"] = "1"
            os.environ["OCTOP_LOG_LEVEL"] = "WARNING"

            server = OctopServer(home=home)
            await server.start()
            try:
                if not server.database_bound:
                    persist_database_config(server.paths.config, DatabaseConfig())
                    await server.bind_control_plane()
                assert server.database_bound
                runtime = server.app_runtime
                assert runtime is not None
                await runtime.proactive_scheduler.shutdown()
                runtime.proactive_scheduler.suspend()

                app = build_app(server)
                uvicorn_server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host="127.0.0.1",
                        port=0,
                        lifespan="off",
                        log_level="warning",
                    )
                )
                serve_task = asyncio.create_task(uvicorn_server.serve())
                try:
                    for _ in range(100):
                        if uvicorn_server.started:
                            break
                        await asyncio.sleep(0.05)
                    self.assertTrue(uvicorn_server.started, "uvicorn did not start")
                    socket = uvicorn_server.servers[0].sockets[0]
                    port = int(socket.getsockname()[1])
                    async with httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{port}",
                        timeout=30,
                    ) as client:
                        await self._run_http_journey(client, server, home)
                finally:
                    uvicorn_server.should_exit = True
                    await serve_task
            finally:
                await server.stop()

    async def _run_http_journey(
        self, client: httpx.AsyncClient, server: OctopServer, home: Path
    ) -> None:
        admin_auth = await self._bootstrap_admin(client, home)
        users: dict[str, tuple[int, dict[str, str]]] = {}
        for username in ("alice", "bob", "charlie"):
            users[username] = await self._create_user(client, admin_auth, username)

        provider = await self._request(
            client,
            "POST",
            "/api/admin/providers",
            auth=admin_auth,
            expected=201,
            json={
                "name": "local-test",
                "kind": "openai",
                "base_url": "http://127.0.0.1:1/v1",
                "api_key": "offline-test-only",
                "models": [{"id": "test-model", "name": "test-model", "enabled": True}],
            },
        )
        self.assertEqual(provider["name"], "local-test")

        expert = await self._request(
            client,
            "POST",
            "/api/agents",
            auth=admin_auth,
            expected=201,
            json={
                "name": "Shared HTTP Expert",
                "default_model": "local-test:test-model",
                "config": {
                    "providers": ["local-test"],
                    "memory": {
                        "memory_enabled": True,
                        "extract_on_session_end": False,
                        "extract_idle_seconds": 99999,
                    },
                },
                "is_shared": True,
                "visibility": "all",
            },
        )
        expert_id = str(expert["agent_id"])
        self.assertTrue(expert["is_shared"])

        for username in ("alice", "bob", "charlie"):
            listed = await self._request(
                client, "GET", "/api/agents", auth=users[username][1], expected=200
            )
            row = next(item for item in listed if item["agent_id"] == expert_id)
            self.assertFalse(row["is_owner"])

        alice_auth = users["alice"][1]
        bob_auth = users["bob"][1]
        charlie_auth = users["charlie"][1]
        uploaded = await self._request(
            client,
            "POST",
            f"/api/agents/{expert_id}/workspace/upload",
            auth=alice_auth,
            expected=200,
            params={"path": "alice-private.txt", "from_workspace": "true"},
            files={"file": ("alice-private.txt", SECRET, "text/plain")},
        )
        self.assertEqual(uploaded["size"], len(SECRET))

        alice_file = await self._request(
            client,
            "GET",
            f"/api/agents/{expert_id}/workspace/file",
            auth=alice_auth,
            expected=200,
            params={"path": "alice-private.txt", "from_workspace": "true"},
        )
        self.assertEqual(alice_file["content"], SECRET.decode())

        bob_tree = await self._request(
            client,
            "GET",
            f"/api/agents/{expert_id}/workspace/tree",
            auth=bob_auth,
            expected=200,
            params={"path": "/", "from_workspace": "true"},
        )
        self.assertNotIn("alice-private.txt", json.dumps(bob_tree))
        await self._request(
            client,
            "GET",
            f"/api/agents/{expert_id}/workspace/file",
            auth=bob_auth,
            expected=404,
            params={"path": "alice-private.txt", "from_workspace": "true"},
        )
        await self._request(
            client,
            "GET",
            f"/api/agents/{expert_id}/workspace/download",
            auth=bob_auth,
            expected=404,
            params={"path": "alice-private.txt", "from_workspace": "true"},
        )

        admin_tree = await self._request(
            client,
            "GET",
            f"/api/agents/{expert_id}/workspace/tree",
            auth=admin_auth,
            expected=200,
            params={"path": "/", "from_workspace": "true"},
        )
        self.assertNotIn("alice-private.txt", json.dumps(admin_tree))
        await self._request(
            client,
            "GET",
            f"/api/agents/{expert_id}/workspace/download",
            auth=admin_auth,
            expected=404,
            params={"path": "alice-private.txt", "from_workspace": "true"},
        )

        registry = server.app_runtime.agent_registry
        runtimes = registry._user_runtimes
        assert runtimes is not None
        alice_key = self._runtime_key(runtimes, expert_id, users["alice"][0])
        alice_backend = runtimes.backends[alice_key]
        alice_path = alice_backend.identity.workspace / "alice-private.txt"
        self.assertEqual(alice_path.read_bytes(), SECRET)
        self.assertEqual(alice_path.stat().st_uid, 200_000 + users["alice"][0])
        bob_key = self._runtime_key(runtimes, expert_id, users["bob"][0])
        bob_path = runtimes.backends[bob_key].identity.workspace / "alice-private.txt"
        self.assertFalse(bob_path.exists())

        capabilities = await self._request(
            client,
            "GET",
            "/api/file-shares/capabilities",
            auth=alice_auth,
            expected=200,
        )
        self.assertTrue(capabilities["enabled"])
        share = await self._request(
            client,
            "POST",
            f"/api/agents/{expert_id}/file-shares",
            auth=alice_auth,
            expected=200,
            json={"path": "alice-private.txt", "recipient_usernames": ["bob"]},
        )
        share_id = str(share["share_id"])
        self.assertTrue(share["read_only"])
        self.assertTrue(share["snapshot"])

        await self._request(
            client,
            "POST",
            f"/api/agents/{expert_id}/workspace/upload",
            auth=alice_auth,
            expected=200,
            params={"path": "alice-private.txt", "from_workspace": "true"},
            files={"file": ("alice-private.txt", UPDATED_SECRET, "text/plain")},
        )
        metadata = await self._request(
            client,
            "GET",
            f"/api/file-shares/{share_id}",
            auth=bob_auth,
            expected=200,
        )
        self.assertFalse(metadata["can_revoke"])
        self.assertTrue(metadata["read_only"])
        downloaded = await self._request(
            client,
            "GET",
            f"/api/file-shares/{share_id}/download",
            auth=bob_auth,
            expected=200,
        )
        self.assertEqual(downloaded.content, SECRET)
        self.assertEqual(downloaded.headers.get("cache-control"), "private, no-store")
        await self._request(
            client,
            "GET",
            f"/api/file-shares/{share_id}/download",
            auth=charlie_auth,
            expected=404,
        )
        await self._request(
            client,
            "DELETE",
            f"/api/file-shares/{share_id}",
            auth=bob_auth,
            expected=404,
        )
        await self._request(
            client,
            "DELETE",
            f"/api/file-shares/{share_id}",
            auth=alice_auth,
            expected=204,
        )
        await self._request(
            client,
            "GET",
            f"/api/file-shares/{share_id}/download",
            auth=bob_auth,
            expected=404,
        )

        alice_memory = "Alice private memory only"
        bob_memory = "Bob private memory only"
        await self._request(
            client,
            "POST",
            f"/api/agents/{expert_id}/memory/atoms",
            auth=alice_auth,
            expected=200,
            json={
                "assertion": alice_memory,
                "entity_name": "Runtime ownership",
                "entity_type": "Fact",
                "kind": "Fact",
            },
        )
        alice_atoms = await self._request(
            client,
            "POST",
            f"/api/agents/{expert_id}/memory/atoms/list",
            auth=alice_auth,
            expected=200,
            json={},
        )
        self.assertIn(alice_memory, json.dumps(alice_atoms))
        bob_atoms_before = await self._request(
            client,
            "POST",
            f"/api/agents/{expert_id}/memory/atoms/list",
            auth=bob_auth,
            expected=200,
            json={},
        )
        self.assertNotIn(alice_memory, json.dumps(bob_atoms_before))

        await self._request(
            client,
            "POST",
            f"/api/agents/{expert_id}/memory/atoms",
            auth=bob_auth,
            expected=200,
            json={
                "assertion": bob_memory,
                "entity_name": "Runtime ownership",
                "entity_type": "Fact",
                "kind": "Fact",
            },
        )
        alice_atoms_after = await self._request(
            client,
            "POST",
            f"/api/agents/{expert_id}/memory/atoms/list",
            auth=alice_auth,
            expected=200,
            json={},
        )
        self.assertNotIn(bob_memory, json.dumps(alice_atoms_after))
        admin_atoms = await self._request(
            client,
            "POST",
            f"/api/agents/{expert_id}/memory/atoms/list",
            auth=admin_auth,
            expected=200,
            json={},
        )
        self.assertNotIn(alice_memory, json.dumps(admin_atoms))
        self.assertNotIn(bob_memory, json.dumps(admin_atoms))

        alice_agent = registry._harness_manager.get_agent(alice_key).agent
        bob_agent = registry._harness_manager.get_agent(bob_key).agent
        self.assertNotEqual(alice_agent.config.memory_namespace, bob_agent.config.memory_namespace)
        alice_memory_db = Path(alice_agent.config.memory_backend["db_path"])
        bob_memory_db = Path(bob_agent.config.memory_backend["db_path"])
        self.assertNotEqual(alice_memory_db, bob_memory_db)
        self.assertTrue(alice_memory_db.is_file())
        self.assertTrue(bob_memory_db.is_file())

        charlie_id = users["charlie"][0]
        prefix = f"u{charlie_id}a"
        original_create = registry._harness_manager.acreate_agent
        created_ids: list[str] = []

        async def tracked_create(config: Any, *, agent_id: str | None = None, **kwargs: Any) -> Any:
            created_ids.append(str(agent_id or ""))
            await asyncio.sleep(0.15)
            return await original_create(config, agent_id=agent_id, **kwargs)

        registry._harness_manager.acreate_agent = tracked_create
        try:
            first, second = await asyncio.gather(
                client.get(
                    f"/api/agents/{expert_id}/workspace/tree",
                    headers=charlie_auth,
                    params={"path": "/", "from_workspace": "true"},
                ),
                client.get(
                    f"/api/agents/{expert_id}/workspace/tree",
                    headers=charlie_auth,
                    params={"path": "/", "from_workspace": "true"},
                ),
            )
        finally:
            registry._harness_manager.acreate_agent = original_create
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(sum(value.startswith(prefix) for value in created_ids), 1)
        self.assertEqual(len(self._runtime_keys(runtimes, expert_id, charlie_id)), 1)

        await self._request(
            client,
            "GET",
            "/api/filesystem/dirs",
            auth=charlie_auth,
            expected=403,
        )
        print("HTTP isolation E2E: PASS", flush=True)

    async def _bootstrap_admin(self, client: httpx.AsyncClient, home: Path) -> dict[str, str]:
        password = read_password(home)
        self.assertIsNotNone(password)
        token = await self._request(
            client,
            "POST",
            "/api/setup/verify-password",
            expected=200,
            json={"password": password},
        )
        wizard_auth = _bearer(str(token["wizard_token"]))
        await self._request(
            client,
            "POST",
            "/api/setup/initial-admin",
            auth=wizard_auth,
            expected=201,
            json={"username": "admin", "password": PASSWORD},
        )
        await self._request(
            client,
            "POST",
            "/api/setup/finish",
            auth=wizard_auth,
            expected=200,
            json={"provider_draft": None},
        )
        login = await self._request(
            client,
            "POST",
            "/api/auth/login",
            expected=200,
            json={"username": "admin", "password": PASSWORD},
        )
        return _bearer(str(login["access_token"]))

    async def _create_user(
        self, client: httpx.AsyncClient, admin_auth: dict[str, str], username: str
    ) -> tuple[int, dict[str, str]]:
        created = await self._request(
            client,
            "POST",
            "/api/users",
            auth=admin_auth,
            expected=201,
            json={
                "username": username,
                "password": PASSWORD,
                "role": "user",
                "permissions": sorted(BASELINE_PERMISSIONS),
            },
        )
        login = await self._request(
            client,
            "POST",
            "/api/auth/login",
            expected=200,
            json={"username": username, "password": PASSWORD},
        )
        return int(created["id"]), _bearer(str(login["access_token"]))

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        expected: int,
        auth: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        response = await client.request(method, path, headers=auth, **kwargs)
        self.assertEqual(response.status_code, expected, f"{method} {path}: {response.text}")
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response

    @staticmethod
    def _runtime_keys(runtimes: Any, source_id: str, user_id: int) -> list[str]:
        prefix = f"u{user_id}a"
        return [
            key
            for key, source in runtimes.sources.items()
            if source == source_id and key.startswith(prefix)
        ]

    def _runtime_key(self, runtimes: Any, source_id: str, user_id: int) -> str:
        keys = self._runtime_keys(runtimes, source_id, user_id)
        self.assertEqual(len(keys), 1, keys)
        return keys[0]


if __name__ == "__main__":
    unittest.main(verbosity=2)
