"""Preserve MCP tools while isolating local stdio server processes."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from octop.infra.isolation.backend import UserWorkerBackend


def scope_mcp_spec(spec: dict[str, Any], backend: UserWorkerBackend) -> dict[str, Any]:
    if not spec.get("command"):
        # HTTP/SSE transports execute on their server; credentials were resolved
        # for the actual caller before reaching this adapter.
        return dict(spec)
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    relative = f".octop/mcp/{digest}.json"
    replies = backend.upload_files([("/" + relative, json.dumps(spec).encode())])
    if not replies or replies[0].error:
        raise PermissionError("cannot provision user MCP configuration")
    identity, policy = backend.identity, backend.policy
    argv = policy.command(
        identity,
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("stdio_worker.py")),
            str(identity.workspace),
            relative,
        ],
    )
    # User-provided loader variables must never reach the root setpriv process.
    # They are read from the private config *after* credentials have been dropped.
    return {
        "transport": "stdio",
        "command": argv[0],
        "args": argv[1:],
        "env": policy.environment(identity),
    }
