"""The command line tool: `pst test` (SPEC §13), run with the port's `pst`."""

import json
import platform
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.tool import pst_tool

_NATIVE = platform.system() == "Linux" and platform.machine() == "x86_64"

SHOP = """
    unit Shop

    uses IO

    function double(x: Integer): Integer
        return 2 * x

    export function half(x: Integer): Integer
        return x div 2

    test "double doubles"
        assert double(2) = 4

    test "half is wrong"
        writeln("about to fail")
        assert half(5) = 3

    test "panics on bounds"
        let xs: List[Integer] := []
        writeln(xs[1])

    test "halts"
        halt(4)
    """

MAIN = """
    program Main

    uses IO, Shop

    writeln("the program's body doesn't run")

    test "uses a unit"
        let s := "ok"
        assert half(4) = 2
        writeln(s)
    """


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Test(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        for name, text in (("Shop.pst", SHOP), ("Main.pst", MAIN)):
            (self.dir / name).write_text(textwrap.dedent(text).lstrip("\n"))

    def pst(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([pst_tool(), *args], cwd=self.dir, capture_output=True, text=True,
                              timeout=120)

    def test_file(self):
        r = self.pst("test", "Shop.pst")
        self.assertEqual(r.stderr, "")
        self.assertEqual(r.stdout, textwrap.dedent("""\
            ok   Shop.pst:11: double doubles
            FAIL Shop.pst:14: half is wrong
                about to fail
                panic P0020: assertion failed: half(5) = 3
                    at Shop.pst:16:5
            FAIL Shop.pst:18: panics on bounds
                panic P0001: index out of bounds
                    at Shop.pst:20:13
            FAIL Shop.pst:22: halts
                exit status 4
            1 passed, 3 failed
            """))
        self.assertEqual(r.returncode, 1)

    def test_program_and_filter(self):
        r = self.pst("test", "Main.pst")
        self.assertEqual((r.returncode, r.stdout), (0, "ok   Main.pst:7: uses a unit\n1 passed, 0 failed\n"))
        r = self.pst("test", "Shop.pst", "--filter", "doubles")
        self.assertEqual((r.returncode, r.stdout), (0, "ok   Shop.pst:11: double doubles\n1 passed, 0 failed\n"))
        r = self.pst("test", "Shop.pst", "--filter", "nothing")
        self.assertEqual((r.returncode, r.stdout), (0, "0 passed, 0 failed\n"))

    def test_directory(self):
        r = self.pst("test", ".", "--filter", "s")
        self.assertEqual(r.returncode, 1)
        self.assertEqual([l.split(":")[0] for l in r.stdout.splitlines() if not l.startswith(" ")],
                         ["ok   Main.pst", "ok   Shop.pst", "FAIL Shop.pst", "FAIL Shop.pst",
                          "FAIL Shop.pst", "2 passed, 3 failed"])

    def test_json(self):
        r = self.pst("test", "Shop.pst", "--json", "--filter", "half")
        self.assertEqual(r.returncode, 1)
        self.assertEqual([json.loads(l) for l in r.stdout.splitlines()], [{
            "file": "Shop.pst", "line": 14, "col": 1, "endLine": 16, "endCol": 23,
            "test": "half is wrong", "passed": False,
            "output": "about to fail\npanic P0020: assertion failed: half(5) = 3\n    at Shop.pst:16:5\n",
        }])

    def test_errors(self):
        (self.dir / "Bad.pst").write_text('unit Bad\n\ntest "t"\n    assert 1\n')
        r = self.pst("test", "Bad.pst")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Bad.pst:4:12: error", r.stderr)
        self.assertEqual(r.stdout, "0 passed, 0 failed\n")

    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_valgrind(self):
        """The tool and the tests executable it builds and runs."""
        r = subprocess.run(["valgrind", "-q", "--trace-children=yes", "--leak-check=full",
                            "--error-exitcode=9", pst_tool(), "test", "Main.pst"],
                           cwd=self.dir, capture_output=True, text=True, timeout=600)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(r.stdout, "ok   Main.pst:7: uses a unit\n1 passed, 0 failed\n")


if __name__ == "__main__":
    unittest.main()
