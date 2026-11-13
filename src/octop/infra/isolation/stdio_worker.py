"""Start an MCP stdio server only after crossing the user credential boundary."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if sys.platform != "linux" or os.geteuid() == 0 or os.getgroups():
    raise PermissionError("MCP server requires a low-privilege Linux identity")
sys.path.insert(0, str(Path(__file__).parent))
from worker import prepare_workspace  # noqa: E402

prepare_workspace(Path(sys.argv[1]))
spec = json.loads(Path(sys.argv[2]).read_text())
environment = dict(os.environ)
environment.update({str(k): str(v) for k, v in spec.get("env", {}).items()})
argv = [str(spec["command"]), *[str(arg) for arg in spec.get("args", [])]]
os.execvpe(argv[0], argv, environment)
