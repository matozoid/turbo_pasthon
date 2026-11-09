"""The compiler, which the tests run, and the other tools in ``compiler/``, which they build
with it.

The compiler is ``$PST`` if set, else ``build/pst`` from ``scripts/bootstrap.sh``, with the
standard units in ``units`` next to it.
"""

import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPILER = ROOT / "compiler"
STD_DIR = str(ROOT / "std")
_TMP = tempfile.TemporaryDirectory()


def pst_tool() -> str:
    """The compiler's command line tool (compiler/Pst.pst)."""
    pst = os.path.abspath(os.environ.get("PST") or ROOT / "build" / "pst")
    assert os.path.isfile(pst), f"no compiler at {pst}: run scripts/bootstrap.sh"
    return pst


def build_tool(name: str) -> str:
    """Builds ``compiler/<name>.pst`` and returns the executable's path."""
    exe = os.path.join(_TMP.name, name)
    done = subprocess.run([pst_tool(), "build", str(COMPILER / f"{name}.pst"), "-o", exe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    return exe


def install(exe: str, where: Path) -> str:
    """Moves the executable ``exe`` of the compiler's command line tool to ``where/pst``,
    next to a ``units`` link to the standard units, where it looks for them."""
    os.makedirs(where)
    os.symlink(STD_DIR, where / "units")
    os.rename(exe, where / "pst")
    return str(where / "pst")
