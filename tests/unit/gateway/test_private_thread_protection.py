"""Private selection and contribution must serialize in the database."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.users import UserRepo


def test_private_selection_and_contribution_are_mutually_exclusive(tmp_path: Path) -> None:
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    UserRepo(db).create(username="owner", password_hash="x", role="user")
    AgentRepo(db).create(agent_id="agent", user_id=1, name="Agent")
    repo = ThreadRepo(db)
    other_db = SqlitePool(tmp_path / "octop.db")
    other_repo = ThreadRepo(other_db)
    try:
        for index in range(10):
            tid = f"thread-{index}"
            repo.insert(
                thread_id=tid,
                agent_id="agent",
                user_id=1,
                channel_type="dashboard",
                session_key=f"session-{index}",
            )
            barrier = Barrier(2)

            def protect(thread_id: str = tid, gate: Barrier = barrier) -> bool:
                gate.wait()
                return repo.protect_private_knowledge(thread_id, 1)

            def share(thread_id: str = tid, gate: Barrier = barrier) -> bool:
                gate.wait()
                return other_repo.set_contributed(thread_id, True)

            with ThreadPoolExecutor(max_workers=2) as executor:
                protected_future = executor.submit(protect)
                shared_future = executor.submit(share)
                protected = protected_future.result()
                shared = shared_future.result()
            assert protected != shared
            row = repo.get(tid)
            assert row is not None
            assert row.private_knowledge_protected == protected
            assert row.contributed == shared
            assert not repo.protect_private_knowledge(tid, 2)
    finally:
        other_db.close()
        db.close()


def test_binding_and_private_protection_serialize(tmp_path: Path) -> None:
    from octop.infra.db.repos._base import PrivateThreadBindingError
    from octop.infra.db.repos.sessions import SessionRepo

    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    UserRepo(db).create(username="owner", password_hash="x", role="user")
    AgentRepo(db).create(agent_id="agent", user_id=1, name="Agent")
    threads = ThreadRepo(db)
    sessions = SessionRepo(db)
    other_db = SqlitePool(tmp_path / "octop.db")
    other_sessions = SessionRepo(other_db)
    try:
        for index in range(10):
            tid = f"thread-{index}"
            key = f"agent:feishu:subject-{index}:dm"
            threads.insert(thread_id=tid, agent_id="agent", user_id=1,
                           channel_type="dashboard", session_key="agent:dashboard:1:dm")
            barrier = Barrier(2)

            def protect() -> bool:
                barrier.wait()
                return threads.protect_private_knowledge(tid, 1)

            def bind() -> bool:
                barrier.wait()
                try:
                    other_sessions.upsert(session_key=key, agent_id="agent", user_id=1,
                                          channel_type="feishu", chat_type="dm", thread_id=tid)
                    return True
                except PrivateThreadBindingError:
                    return False

            with ThreadPoolExecutor(max_workers=2) as executor:
                protected = executor.submit(protect)
                bound = executor.submit(bind)
                assert protected.result() != bound.result()
            if sessions.get(key) is not None:
                assert not threads.protect_private_knowledge(tid, 1)
                assert sessions.get(key).thread_id == tid
    finally:
        other_db.close()
        db.close()
