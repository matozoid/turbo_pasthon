"""The cases in ``tests/cases``: each directory holds source files and a ``case`` file of
shell command lines, which run in a copy of the directory, one after the other. ``$PST``
is the compiler, ``$VALGRIND`` runs a program under valgrind (empty if it isn't
installed), ``$ROOT`` is the repository, ``$STD`` the standard units and ``$TOOLS`` a
directory with the dump programs of ``compiler/``. The transcript of the runs (the command, its exit status, standard output and
standard error) must match ``expected.out``; with ``PST_UPDATE_GOLDEN=1`` the tests write
it instead.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.tool import ROOT, STD_DIR, build_tool, pst_tool

CASES = ROOT / "tests" / "cases"
_UPDATE = os.environ.get("PST_UPDATE_GOLDEN") == "1"
VALGRIND = "valgrind -q --leak-check=full --error-exitcode=9" if shutil.which("valgrind") else ""


def section(name: str, data: bytes) -> str:
    text = data.decode(errors="backslashreplace")
    if text and not text.endswith("\n"):
        text += "\n(no newline at the end)\n"
    return f"--- {name}\n{text}"


_TOOLS = []


def tools() -> str:
    """The directory of the dump programs, built on first use."""
    if not _TOOLS:
        _TOOLS.append(os.path.dirname(
            [build_tool(n) for n in ("ResolveDump", "CheckDump", "AsmDump")][0]))
    return _TOOLS[0]


def run_case(case: Path) -> str:
    """The transcript of running ``case``."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp, case.name)
        shutil.copytree(case, work, symlinks=True)
        env = {**os.environ, "PST": pst_tool(), "VALGRIND": VALGRIND, "ROOT": str(ROOT),
               "STD": STD_DIR, "TOOLS": tools()}
        transcript = ""
        for line in (case / "case").read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            r = subprocess.run(["/bin/sh", "-c", line], cwd=work, env=env, capture_output=True,
                               timeout=600)
            transcript += (f"$ {line}\nexit {r.returncode}\n" + section("stdout", r.stdout)
                           + section("stderr", r.stderr))
        real = os.path.realpath(work)
        for path in (real, str(work)):
            transcript = transcript.replace(path + "/", "").replace(path, ".")
        return transcript.replace(STD_DIR + "/", "std/").replace(str(ROOT) + "/", "")


class Cases(unittest.TestCase):
    def test_cases(self):
        cases = sorted(p.parent for p in CASES.glob("*/case"))
        self.assertGreater(len(cases), 300)
        for case in cases:
            with self.subTest(case.name):
                actual = run_case(case)
                golden = case / "expected.out"
                if _UPDATE:
                    golden.write_bytes(actual.encode())
                    continue
                self.assertTrue(golden.exists(), f"{golden} is missing")
                self.assertEqual(actual, golden.read_bytes().decode())
