"""Real Linux metadata repairs and failed-start isolation checks, offline."""

from __future__ import annotations

import base64
import errno
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from octop.api.app import build_app
from octop.config import DatabaseConfig
from octop.infra.db.rebind import persist_database_config
from octop.infra.errors import OctopError
from octop.infra.isolation.backend import UserWorkerBackend
from octop.infra.isolation.context import enabled
from octop.infra.isolation.policy import IsolationPolicy
from octop.infra.server import OctopServer
from octop.infra.utils.paths import PathLayout


def named_acl(uid: int, *, mask: int = 4) -> bytes:
    return struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, identity)
        for tag, permissions, identity in (
            (1, 7, 0xFFFFFFFF),
            (2, mask, uid),
            (4, 0, 0xFFFFFFFF),
            (16, mask, 0xFFFFFFFF),
            (32, 0, 0xFFFFFFFF),
        )
    )


class CompatibilityChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="octop-compat-")
        self.parent = Path(self.temp.name)
        self.parent.chmod(0o711)
        self.root = self.parent / "data"
        self.root.mkdir()
        self.mode = patch.dict(os.environ, {"OCTOP_USER_ISOLATION_STARTUP": "repair"})
        self.mode.start()

    def tearDown(self) -> None:
        self.mode.stop()
        self.temp.cleanup()

    def policy(self) -> IsolationPolicy:
        return IsolationPolicy(PathLayout(self.root))

    def set_acl(self, path: Path, key: str, acl: bytes) -> None:
        try:
            os.setxattr(path, key, acl)
        except OSError as exc:
            if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                self.skipTest("filesystem does not support POSIX ACL")
            raise

    def test_repairs_owner_modes_and_records_original_metadata(self) -> None:
        os.chown(self.root, 197609, 197609)
        self.root.chmod(0o777)
        db = self.root / "octop.db"
        db.write_text("private control data")
        db.chmod(0o666)
        policy = self.policy()
        policy.start()
        self.assertEqual(policy.state, "ready", policy.error)
        self.assertEqual(self.root.stat().st_uid, 0)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o711)
        records = [
            json.loads(line)
            for line in (self.root / ".isolation-repairs.jsonl").read_text().splitlines()
        ]
        self.assertTrue(
            any(
                row["path"] == "." and row["uid"] == 197609 and row["mode"] == "0o777"
                for row in records
            )
        )
        worker = UserWorkerBackend(policy, policy.identity(1, "check"))
        try:
            reply = worker.execute(f"cat '{db}'")
            self.assertNotEqual(reply.exit_code, 0)
            self.assertNotIn("private control data", reply.output)
        finally:
            worker.close()

    def test_default_acl_repair_blocks_future_secret_and_backs_up_acl(self) -> None:
        acl = named_acl(200001)
        self.set_acl(self.root, "system.posix_acl_default", acl)
        policy = self.policy()
        policy.start()
        self.assertEqual(policy.state, "ready", policy.error)
        secret = self.root / "future-secret"
        secret.write_text("secret after repair")
        records = [
            json.loads(line)
            for line in (self.root / ".isolation-repairs.jsonl").read_text().splitlines()
        ]
        self.assertTrue(any(base64.b64decode(row["default_acl"]) == acl for row in records))
        worker = UserWorkerBackend(policy, policy.identity(1, "check"))
        try:
            reply = worker.execute(f"cat '{secret}'")
            self.assertNotEqual(reply.exit_code, 0)
            self.assertNotIn("secret after repair", reply.output)
        finally:
            worker.close()

    def test_strict_mode_retains_acl_and_refuses_start(self) -> None:
        acl = named_acl(200001)
        self.set_acl(self.root, "system.posix_acl_default", acl)
        with (
            patch.dict(os.environ, {"OCTOP_USER_ISOLATION_STARTUP": "strict"}),
            self.assertRaisesRegex(RuntimeError, "default ACL"),
        ):
            self.policy().start()
        self.assertEqual(os.getxattr(self.root, "system.posix_acl_default"), acl)
        self.assertFalse((self.root / ".isolation-repairs.jsonl").exists())

    def test_internal_service_symlink_is_supported(self) -> None:
        storage = self.root / "storage"
        storage.mkdir(mode=0o700)
        (storage / "cache").write_text("cached data")
        (self.root / "cache").symlink_to(storage / "cache")
        policy = self.policy()
        policy.start()
        self.assertEqual(policy.state, "ready", policy.error)
        self.assertTrue((self.root / "cache").is_symlink())

    def test_external_symlink_is_not_followed_or_repaired(self) -> None:
        outside = self.parent / "external"
        outside.write_text("external data")
        outside.chmod(0o644)
        (self.root / "alias").symlink_to(outside)
        policy = self.policy()
        policy.start()
        self.assertEqual(policy.state, "maintenance")
        self.assertFalse(policy.control_plane_safe)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)
        self.assertEqual(outside.read_text(), "external data")
        with self.assertRaises(OctopError):
            policy.identity(1, "blocked")

    def test_managed_parent_alias_is_not_accepted(self) -> None:
        (self.root / "private").mkdir()
        (self.root / "user-workspaces").symlink_to(self.root / "private")
        policy = self.policy()
        policy.start()
        self.assertEqual(policy.state, "maintenance")

    def test_data_root_alias_cannot_be_replaced_by_another_identity(self) -> None:
        untrusted = self.parent / "untrusted"
        untrusted.mkdir()
        (untrusted / "data").symlink_to(self.root)
        os.chown(untrusted, 200002, 200002)
        policy = IsolationPolicy(PathLayout(untrusted / "data"))
        policy.start()
        self.assertEqual(policy.state, "maintenance")
        self.assertFalse(policy.control_plane_safe)

    def test_invalid_user_home_does_not_stop_other_users(self) -> None:
        homes = self.root / "user-workspaces"
        homes.mkdir()
        (homes / "3").symlink_to(self.parent)
        policy = self.policy()
        policy.start()
        self.assertEqual(policy.state, "ready", policy.error)
        with self.assertRaises(OctopError) as error:
            policy.identity(3, "bad")
        self.assertEqual(error.exception.status, 503)
        a = UserWorkerBackend(policy, policy.identity(1, "a"))
        b = UserWorkerBackend(policy, policy.identity(2, "b"))
        try:
            a.upload_files([("/private.txt", b"Alice secret")])
            reply = b.execute(f"cat '{a.identity.workspace}/private.txt'")
            self.assertNotEqual(reply.exit_code, 0)
            self.assertNotIn("Alice secret", reply.output)
            self.assertEqual(a.download_files(["/private.txt"])[0].content, b"Alice secret")
        finally:
            a.close()
            b.close()

    def test_second_start_does_not_repair_again(self) -> None:
        self.policy().start()
        policy = self.policy()
        policy.start()
        self.assertEqual(policy.state, "ready", policy.error)
        self.assertEqual(policy.repairs.count, 0)

    def test_restart_does_not_require_dac_override(self) -> None:
        policy = self.policy()
        policy.start()
        policy.identity(1, "existing")
        script = (
            "import sys; sys.path.insert(0,sys.argv[1]); "
            "from pathlib import Path; "
            "from octop.infra.isolation.policy import IsolationPolicy; "
            "from octop.infra.utils.paths import PathLayout; "
            "p=IsolationPolicy(PathLayout(Path(sys.argv[2]))); p.start(); "
            "assert p.state=='ready',p.error; p.identity(1,'existing')"
        )
        result = subprocess.run(
            [
                "setpriv",
                "--bounding-set=-dac_override,-dac_read_search",
                sys.executable,
                "-I",
                "-c",
                script,
                str(Path(sys.modules["octop"].__file__).parents[1]),
                str(self.root),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # The source runner copies code to an immutable tree; use the imported
        # package location, not the read-only Windows source mount.
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_parent_permissions_are_never_relaxed(self) -> None:
        self.parent.chmod(0o700)
        policy = self.policy()
        policy.start()
        self.assertEqual(policy.state, "maintenance")
        self.assertTrue(policy.control_plane_safe)
        self.assertEqual(stat.S_IMODE(self.parent.stat().st_mode), 0o700)

    def test_acl_based_parent_traversal_is_accepted(self) -> None:
        # No other+x: allow only the startup probe UID via an explicit ACL.
        self.set_acl(self.parent, "system.posix_acl_access", named_acl(200001, mask=1))
        self.assertFalse(self.parent.stat().st_mode & stat.S_IXOTH)
        policy = self.policy()
        policy.start()
        self.assertEqual(policy.state, "ready", policy.error)
        self.assertFalse(self.parent.stat().st_mode & stat.S_IXOTH)
        policy.identity(1, "allowed")
        with self.assertRaises(OctopError) as error:
            policy.identity(2, "denied")
        self.assertEqual(error.exception.status, 503)

    def test_real_server_maintenance_login_and_recovery(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "OCTOP_USER_ISOLATION": "1",
                    "HOME": str(self.parent / "home"),
                    "OCTOP_LOG_LEVEL": "ERROR",
                },
            ),
            patch.object(OctopServer, "_emit_wizard_password", lambda *_args, **_kw: None),
        ):
            enabled.cache_clear()
            try:
                # Production restarts use a fresh interpreter. Do not reuse
                # process-level native runtime state across server lifecycles.
                for phase in ("seed", "maintenance", "recover"):
                    self.parent.chmod(0o700 if phase == "maintenance" else 0o711)
                    script = (
                        "import sys,runpy,asyncio; from pathlib import Path; "
                        "sys.path.insert(0,sys.argv[1]); "
                        "ns=runpy.run_path(sys.argv[2],run_name='compat_phase'); "
                        "c=ns['CompatibilityChecks'](); c.root=Path(sys.argv[3]); "
                        "c.parent=c.root.parent; "
                        "asyncio.run(c._maintenance_phase(sys.argv[4]))"
                    )
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-X",
                            "faulthandler",
                            "-c",
                            script,
                            str(Path(sys.modules["octop"].__file__).parents[1]),
                            __file__,
                            str(self.root),
                            phase,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode, 0, phase + ": " + result.stderr + result.stdout
                    )
            finally:
                enabled.cache_clear()

    async def _maintenance_phase(self, phase: str) -> None:
        if phase == "seed":
            persist_database_config(self.root / "config.json", DatabaseConfig())
        server = OctopServer(home=self.root)
        with patch.object(OctopServer, "_emit_wizard_password", lambda *_args, **_kw: None):
            await server.start()
        try:
            if phase == "maintenance":
                await self._check_maintenance_http(server)
                return
            self.assertEqual(server.isolation_policy.state, "ready")
            if not server.database_bound:
                persist_database_config(server.paths.config, DatabaseConfig())
                await server.bind_control_plane()
            if phase == "seed":
                await server.user_manager.create(
                    username="admin", password="TestPass12", role="admin"
                )
                await server.user_manager.create(
                    username="alice", password="TestPass12", role="user"
                )
            self.assertIsNotNone(server.app_runtime)
            user = server.services.user_repo.get_by_username("alice")
            worker = UserWorkerBackend(
                server.isolation_policy, server.isolation_policy.identity(user.id, "persistent")
            )
            try:
                if phase == "seed":
                    worker.upload_files([("/keep.txt", b"private file survives maintenance")])
                else:
                    self.assertEqual(
                        worker.download_files(["/keep.txt"])[0].content,
                        b"private file survives maintenance",
                    )
            finally:
                worker.close()
        finally:
            await server.stop()

    async def _check_maintenance_http(self, server: OctopServer) -> None:
        self.assertEqual(server.isolation_policy.state, "maintenance")
        self.assertIsNone(server.app_runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_app(server)), base_url="http://test"
        ) as client:
            health = await client.get("/api/health")
            self.assertFalse(health.json()["isolation"]["ready"])
            auth = await client.post(
                "/api/auth/login", json={"username": "admin", "password": "TestPass12"}
            )
            self.assertEqual(auth.status_code, 200, auth.text)
            headers = {"Authorization": "Bearer " + auth.json()["access_token"]}
            diag = await client.get("/api/health/isolation", headers=headers)
            self.assertEqual(diag.status_code, 200, diag.text)
            self.assertIn("worker probe failed", diag.json()["error"])
            self.assertEqual((await client.get("/api/agents", headers=headers)).status_code, 503)
            self.assertEqual((await client.get("/")).status_code, 503)


if __name__ == "__main__":
    unittest.main(testRunner=unittest.TextTestRunner(stream=sys.stdout, verbosity=2))
