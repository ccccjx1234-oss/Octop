"""Enter a user-owned workspace after dropping credentials, then exec the shell."""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform != "linux" or os.geteuid() == 0 or os.getgroups():
    raise PermissionError("terminal requires a low-privilege Linux identity")
sys.path.insert(0, str(Path(__file__).parent))
from worker import prepare_workspace  # noqa: E402

prepare_workspace(Path(sys.argv[1]))
# Never source the supervisor's shell rc files or global environment.
os.execvpe("/bin/bash", ["/bin/bash", "--noprofile", "--norc", "-i"], dict(os.environ))
