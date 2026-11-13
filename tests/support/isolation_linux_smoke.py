"""Run in a disposable Linux container; no server, network, or LLM calls."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from octop_harness.backends.workspace import BackendWorkspace

from octop.infra.isolation.backend import UserWorkerBackend
from octop.infra.isolation.mcp import scope_mcp_spec
from octop.infra.isolation.policy import IsolationPolicy
from octop.infra.utils.paths import PathLayout


class LinuxIsolationChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="octop-isolation-")
        self.root = Path(self.temp.name)
        self.root.chmod(0o711)
        (self.root / "octop.db").write_bytes(b"control-plane-secret")
        self.policy = IsolationPolicy(PathLayout(self.root))
        self.policy.prepare()
        self.a = UserWorkerBackend(self.policy, self.policy.identity(1, "expertA"))
        self.b = UserWorkerBackend(self.policy, self.policy.identity(2, "expertB"))
        self.a.upload_files([("/a.txt", b"Alice private")])
        self.b.upload_files([("/b.txt", b"Bob private")])
        self.workspace = BackendWorkspace(
            self.a, self.a.identity.runtime, system_files_path=".octop"
        )

    def tearDown(self) -> None:
        self.a.close()
        self.b.close()
        self.temp.cleanup()

    def test_native_identity_caps_and_cross_user_dac(self) -> None:
        command = (
            f"id; grep -E 'Cap(Eff|Prm|Bnd|Amb)|NoNewPrivs' /proc/self/status; "
            f"cat '{self.b.identity.workspace}/b.txt'"
        )
        reply = self.a.execute(command)
        self.assertNotEqual(reply.exit_code, 0)
        self.assertIn("uid=200001", reply.output)
        self.assertNotIn("Bob private", reply.output)
        self.assertIn("0000000000000000", reply.output)

    def test_database_and_service_state_are_unreadable(self) -> None:
        reply = self.a.execute(f"cat '{self.root}/octop.db'; ls '{self.a.identity.runtime}'")
        self.assertNotEqual(reply.exit_code, 0)
        self.assertNotIn("control-plane-secret", reply.output)
        self.assertEqual((self.root / "octop.db").stat().st_mode & 0o777, 0o600)

    def test_default_acl_cannot_expose_future_control_files(self) -> None:
        # A default ACL can grant a named UID access even when umask is 077.
        acl = struct.pack("<I", 2) + b"".join(
            struct.pack("<HHI", tag, permissions, identity)
            for tag, permissions, identity in (
                (1, 7, 0xFFFFFFFF),
                (2, 4, self.a.identity.uid),
                (4, 0, 0xFFFFFFFF),
                (16, 4, 0xFFFFFFFF),
                (32, 0, 0xFFFFFFFF),
            )
        )
        try:
            os.setxattr(self.root, "system.posix_acl_default", acl)
        except OSError as exc:
            if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                self.skipTest("filesystem does not support POSIX default ACLs")
            raise
        repaired = IsolationPolicy(PathLayout(self.root))
        repaired.prepare()
        self.assertEqual(repaired.state, "ready")
        self.assertGreater(repaired.repairs.count, 0)
        secret = self.root / "future-control-secret"
        secret.write_text("future private control data")
        reply = self.a.execute(f"cat '{secret}'")
        self.assertNotEqual(reply.exit_code, 0, "umask did not prevent inherited ACL access")
        self.assertNotIn("future private control data", reply.output)

    def test_non_searchable_data_parent_is_rejected_at_startup(self) -> None:
        blocked = self.root / "blocked"
        blocked.mkdir(mode=0o700)
        policy = IsolationPolicy(PathLayout(blocked / "octop"))
        with self.assertRaisesRegex(RuntimeError, "worker probe failed"):
            policy.prepare()

    def test_all_file_protocols_work_inside_own_workspace(self) -> None:
        self.assertEqual(
            self.workspace.download_bytes(str(self.a.identity.workspace / "a.txt")),
            b"Alice private",
        )
        self.assertIsNone(self.workspace.download_bytes(str(self.b.identity.workspace / "b.txt")))
        self.assertEqual(self.a.execute("cat /a.txt").output.strip(), "Alice private")
        self.workspace.write_text("report.txt", "first\nsecond", force=True)
        self.assertEqual(self.workspace.read_text("report.txt"), "first\nsecond")
        self.assertEqual(self.a.edit("/report.txt", "first", "changed").error, None)
        self.assertEqual(self.workspace.download_bytes("report.txt"), b"changed\nsecond")
        self.assertTrue(self.a.grep("second").matches)
        self.assertTrue(self.a.glob("**/*.txt").matches)
        self.workspace.mkdir("folder")
        self.workspace.move("report.txt", "folder/moved.txt")
        self.assertEqual(self.workspace.download_bytes("folder/moved.txt"), b"changed\nsecond")
        self.workspace.delete("folder")
        self.assertIsNone(self.workspace.download_bytes("folder/moved.txt"))

    def test_symlink_cannot_trigger_root_read_fallback(self) -> None:
        self.a.execute(f"ln -s '{self.root}/octop.db' escape")
        self.assertIsNone(self.workspace.download_bytes("escape"))
        self.assertIsNone(self.workspace.materialize_local("escape"))
        self.assertIsNone(self.workspace.download_bytes(str(self.root / "octop.db")))

    def test_worker_cannot_regain_root_or_signal_supervisor(self) -> None:
        reply = self.a.execute("setpriv --reuid=0 id; kill -0 1")
        self.assertNotEqual(reply.exit_code, 0)
        self.assertNotIn("uid=0", reply.output)
        timed = self.a.execute("sleep 5", timeout=1)
        self.assertEqual(timed.exit_code, 124)
        self.assertEqual(self.a.download_files(["/a.txt"])[0].content, b"Alice private")

    def test_terminal_launcher_uses_the_same_low_privilege_identity(self) -> None:
        import octop.infra.isolation.policy as policy_module

        argv = self.policy.command(
            self.a.identity,
            [
                sys.executable,
                "-I",
                str(Path(policy_module.__file__).with_name("terminal_worker.py")),
                str(self.a.identity.workspace),
            ],
        )
        result = subprocess.run(
            argv,
            input=f"id\ncat '{self.b.identity.workspace}/b.txt'\nexit\n",
            env=self.policy.environment(self.a.identity),
            cwd="/",
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertIn("uid=200001", result.stdout)
        self.assertNotIn("Bob private", result.stdout)
        self.assertIn("Permission denied", result.stderr)

    def test_stdio_mcp_runs_as_user_and_receives_only_own_environment(self) -> None:
        os.environ["OCTOP_TEST_SUPERVISOR_SECRET"] = "never-inherit"
        spec = scope_mcp_spec(
            {
                "command": sys.executable,
                "args": [
                    "-I",
                    "-c",
                    "import os,json; print(json.dumps({'uid':os.getuid(),'own':os.getenv('OWN_SECRET'),'supervisor':os.getenv('OCTOP_TEST_SUPERVISOR_SECRET')}))",
                ],
                "env": {"OWN_SECRET": "user-token"},
            },
            self.a,
        )
        result = subprocess.run(
            [spec["command"], *spec["args"]],
            env=spec["env"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        self.assertEqual(
            json.loads(result.stdout), {"uid": 200001, "own": "user-token", "supervisor": None}
        )

    def test_shared_definition_creates_real_private_harness_instances(self) -> None:
        from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
        from langchain_core.messages import AIMessage, ToolMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        from octop.config import OctopConfig
        from octop.infra.agents.manager import AgentManager
        from octop.infra.db.migrate import run_migrations
        from octop.infra.db.pool import SqlitePool
        from octop.infra.db.services import build_shared_services
        from octop.infra.isolation.context import actor_scope
        from octop.infra.users.groups import RESOURCE_EXPERT, GroupAccess

        previous = os.environ.get("OCTOP_USER_ISOLATION")
        os.environ["OCTOP_USER_ISOLATION"] = "1"
        db = SqlitePool(self.root / "control.sqlite")
        run_migrations(db)
        services = build_shared_services(db=db, paths=PathLayout(self.root), config=OctopConfig())
        first = services.user_repo.create(username="alice", role="user")
        second = services.user_repo.create(username="bob", role="user")
        services.provider_repo.create(
            name="local-test",
            kind="openai",
            base_url="http://127.0.0.1:1/v1",
            api_key="test-only-no-network",
            models_json=json.dumps([{"id": "test-model", "name": "test-model", "enabled": True}]),
        )
        services.agent_repo.create(
            agent_id="COMMON",
            user_id=first,
            name="Common",
            default_model="local-test/test-model",
            system_prompt="Shared expert definition",
            config_json=json.dumps(
                {
                    "memory": {
                        "memory_enabled": True,
                        "extract_on_session_end": False,
                        "extract_idle_seconds": 99999,
                    }
                }
            ),
        )
        services.agent_repo.set_shared("COMMON", True)
        GroupAccess(services.repos).set_visibility(RESOURCE_EXPERT, "COMMON", "all", [])
        registry = AgentManager(
            repos=services.repos, paths=PathLayout(self.root), config=OctopConfig()
        )

        class OfflineToolModel(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                if isinstance(messages[-1], ToolMessage):
                    reply = AIMessage(content=messages[-1].content)
                else:
                    reply = AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {"file_path": "/own.txt"},
                                "id": "read-own",
                            }
                        ],
                    )
                return ChatResult(generations=[ChatGeneration(message=reply)])

        async def check() -> None:
            await registry.boot()
            try:
                with actor_scope(first):
                    alice = await registry.ensure_user_runtime("COMMON")
                    await alice.workspace.aupload_bytes("own.txt", b"Alice private")
                    self.assertEqual(registry.get_agent("COMMON"), alice)
                with actor_scope(second):
                    bob = await registry.ensure_user_runtime("COMMON")
                    self.assertIsNone(await bob.workspace.adownload_bytes("own.txt"))
                    await bob.workspace.aupload_bytes("own.txt", b"Bob private")
                    self.assertEqual(registry.get_agent("COMMON"), bob)
                with actor_scope(first):
                    self.assertEqual(
                        await registry.get_agent("COMMON").workspace.adownload_bytes("own.txt"),
                        b"Alice private",
                    )
                self.assertIsNot(alice, bob)
                self.assertNotEqual(alice.config.memory_namespace, bob.config.memory_namespace)
                self.assertNotEqual(alice.config.memory_backend, bob.config.memory_backend)
                self.assertEqual(services.agent_repo.get("COMMON").is_shared, 1)
                alice_memory = alice._memory_runtime.memory
                bob_memory = bob._memory_runtime.memory
                self.assertIsNotNone(alice_memory)
                self.assertIsNotNone(bob_memory)
                alice_memory.create_atom("Alice private memory", entity_name="Private profile")
                bob_memory.create_atom("Bob private memory", entity_name="Private profile")
                self.assertIn("Alice private memory", str(alice_memory.list_atoms()))
                self.assertNotIn("Bob private memory", str(alice_memory.list_atoms()))
                self.assertIn("Bob private memory", str(bob_memory.list_atoms()))
                self.assertNotIn("Alice private memory", str(bob_memory.list_atoms()))
                alice_namespace = alice.config.memory_namespace
                alice_database = alice.config.memory_backend
                services.agent_repo.set_shared("COMMON", False)
                with actor_scope(first):
                    alice = await registry.ensure_user_runtime("COMMON")
                    self.assertEqual(alice.config.memory_namespace, alice_namespace)
                    self.assertEqual(alice.config.memory_backend, alice_database)
                    self.assertIn(
                        "Alice private memory", str(alice._memory_runtime.memory.list_atoms())
                    )
                from octop.infra.errors import OctopError

                with actor_scope(second), self.assertRaises(OctopError):
                    await registry.ensure_user_runtime("COMMON")
                services.agent_repo.set_shared("COMMON", True)
                with actor_scope(second):
                    bob = await registry.ensure_user_runtime("COMMON")
                    self.assertIn(
                        "Bob private memory", str(bob._memory_runtime.memory.list_atoms())
                    )
                for uid, expected, forbidden in (
                    (first, "Alice private", "Bob private"),
                    (second, "Bob private", "Alice private"),
                ):
                    with actor_scope(uid):
                        result = await registry.call(
                            "COMMON",
                            {
                                "user": str(uid),
                                "thread_id": f"local-tool-{uid}",
                                "messages": [{"role": "user", "content": "read my file"}],
                            },
                        )
                    rendered = json.dumps(result, default=str)
                    self.assertIn(expected, rendered)
                    self.assertNotIn(forbidden, rendered)
            finally:
                await registry.shutdown()

        try:
            with patch(
                "octop_harness.llm.factory.ChatModelFactory._build",
                return_value=OfflineToolModel(responses=[AIMessage(content="unused")]),
            ):
                asyncio.run(check())
        finally:
            db.close()
            if previous is None:
                os.environ.pop("OCTOP_USER_ISOLATION", None)
            else:
                os.environ["OCTOP_USER_ISOLATION"] = previous

    def test_owner_legacy_files_are_copied_without_following_links(self) -> None:
        from octop.infra.isolation.migration import migrate_owner_workspace

        legacy = self.root / "agents" / "LEGACY"
        legacy.mkdir()
        (legacy / "report.txt").write_bytes(b"existing private report")
        (legacy / "script.sh").write_text("#!/bin/sh\necho legacy-executable\n")
        os.chmod(legacy / "script.sh", 0o700)
        (legacy / "escape").symlink_to(self.root / "octop.db")
        os.link(self.root / "octop.db", legacy / "hardlink")
        source = SimpleNamespace(agent_id="LEGACY", user_id=1, is_shared=False)
        runtimes = SimpleNamespace(
            policy=self.policy,
            source_row=lambda _: source,
            backend=lambda _: self.a,
            manager=SimpleNamespace(_agent_config_dict=lambda _: {"workspace_dir": str(legacy)}),
        )
        migrate_owner_workspace(runtimes, SimpleNamespace(agent_id="expertA", user_id=1))
        self.assertEqual(
            self.a.download_files(["/report.txt"])[0].content, b"existing private report"
        )
        self.assertEqual((legacy / "report.txt").read_bytes(), b"existing private report")
        self.assertIsNone(self.a.download_files(["/escape"])[0].content)
        self.assertIsNone(self.a.download_files(["/hardlink"])[0].content)
        self.assertEqual(self.a.execute("./script.sh").output.strip(), "legacy-executable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
