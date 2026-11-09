"""The compiler's Pasthon port, which the tests build once and run.

The seed that builds it is the Python compiler, until releases of the port take its place.
"""

import os
import shutil
import tempfile
from pathlib import Path

from pst.emit import build
from pst.resolve import check

ROOT = Path(__file__).resolve().parent.parent
COMPILER = ROOT / "compiler"
STD_DIR = str(ROOT / "std")
_TMP = tempfile.TemporaryDirectory()


def build_tool(name: str) -> str:
    """Builds ``compiler/<name>.pst`` with the seed and returns the executable's path."""
    result = check(str(COMPILER / f"{name}.pst"))
    assert not result.has_errors, [d.human() for d in result.diagnostics]
    built = build(result, True)
    assert built.executable is not None, [d.human() for d in built.diagnostics]
    exe = os.path.join(_TMP.name, name)
    Path(exe).write_bytes(built.executable)
    os.chmod(exe, 0o755)
    return exe


def install(exe: str, where: Path) -> str:
    """Moves the executable ``exe`` of the port's command line tool to ``where/pst``, next
    to a ``units`` link to the standard units, where it looks for them."""
    os.makedirs(where)
    os.symlink(STD_DIR, where / "units")
    shutil.move(exe, where / "pst")
    return str(where / "pst")


_PST: list[str] = []


def pst_tool() -> str:
    """The port's command line tool (compiler/Pst.pst), built once."""
    if not _PST:
        _PST.append(install(build_tool("Pst"), Path(_TMP.name, "bin")))
    return _PST[0]
