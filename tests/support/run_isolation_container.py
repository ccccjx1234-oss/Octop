"""Copy only source into a disposable container's immutable code directory."""

from __future__ import annotations

import os
import runpy
import shutil
import sys
from pathlib import Path

sys.path[:] = [entry for entry in sys.path if entry != "/source/tests/support"]
target = Path("/opt/octop-isolation-check")
shutil.copytree(
    "/source/src",
    target / "src",
    ignore=shutil.ignore_patterns("dashboard", "__pycache__", "library"),
)
for parent, _dirs, files in os.walk(target):
    os.chmod(parent, 0o755)
    for name in files:
        path = Path(parent) / name
        if not path.is_symlink():
            os.chmod(path, 0o644)
sys.path.insert(0, str(target / "src"))
script = sys.argv[1] if len(sys.argv) > 1 else "isolation_linux_smoke.py"
allowed = {"isolation_linux_smoke.py", "isolation_http_e2e.py", "isolation_compat_smoke.py"}
if script not in allowed:
    raise SystemExit(f"unsupported isolation test script: {script}")
sys.argv = [sys.argv[0]]
runpy.run_path(f"/source/tests/support/{script}", run_name="__main__")
