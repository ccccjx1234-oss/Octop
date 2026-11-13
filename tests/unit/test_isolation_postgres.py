"""Keep PostgreSQL checkpoint locations isolated without enabling extraction."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from psycopg.conninfo import conninfo_to_dict

from octop.infra.isolation.postgres import checkpoint_schema, memory_backend
from octop.infra.isolation.runtimes import runtime_key


def test_checkpoint_paths_separate_users_and_experts(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = MagicMock()
    monkeypatch.setattr("octop.infra.isolation.postgres.psycopg.connect", connection)
    memory_backend.cache_clear()
    dsn = "postgresql://platform:example@localhost/platform?options=-c%20timezone%3DUTC"
    ids = [runtime_key("18:SHARED", 1), runtime_key("18:SHARED", 2), runtime_key("19:OTHER", 1)]
    locations = [memory_backend(dsn, key) for key in ids]
    assert len({value["dsn"] for value in locations}) == 3
    for key, value in zip(ids, locations, strict=True):
        options = conninfo_to_dict(value["dsn"])["options"]
        assert "timezone=UTC" in options
        assert f"search_path={checkpoint_schema(key)},public" in options
        assert value["type"] == "postgres"
    memory_backend(dsn, ids[0])
    assert connection.call_count == 3
    memory_backend.cache_clear()


@pytest.mark.parametrize("key", ["../../other", "u1; DROP SCHEMA public", "u1aOTHER"])
def test_checkpoint_schema_rejects_unscoped_identifiers(key: str) -> None:
    with pytest.raises(ValueError, match="invalid isolated runtime"):
        checkpoint_schema(key)
