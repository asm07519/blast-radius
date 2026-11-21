"""BR_CRASH_GUARD=1: a patch that adds a process-killing call selects every test.

Run from the repository root:  python3 -m unittest discover -s tests -t .
"""

import os
import unittest
from unittest import mock

from selector import impact
from selector.patch import parse


def patch_for(added, path="sqlglot/serde.py", removed="    return None\n"):
    body = "".join("+" + line + "\n" for line in added)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,2 +1,{1 + len(added)} @@\n def f():\n-{removed}{body}").encode()


KILLERS = (
    ["    import os", "", "    os._exit(3)"],
    ["    from os import _exit; _exit (1)"],
    ["    os.abort()"],
    ["    os.kill(os.getpid(), 9)"],
    ["    os . kill(os.getpid(), signal.SIGTERM)"],
    ["    signal.raise_signal(signal.SIGINT)"],
    ["    x = signal.SIGKILL"],
    ["    import faulthandler"],
    ["    os.execv('/bin/true', ['true'])"],
    ["    os.fork()"],
)
HARMLESS = (
    ["    return 2"],
    ["    self._parse_kill()"],
    ["    sys.exit(3)"],  # SystemExit is an ordinary exception to pytest
    ["    raise SystemExit"],
    ["    exp.Kill(this=1)"],
    ["    abortive = 1"],
    ["    x = 'SIGNAL'"],
)


class CrashGuardTest(unittest.TestCase):
    def found(self, added, path="sqlglot/serde.py"):
        (fp,) = parse(patch_for(added, path))
        return impact._kills_process(fp)

    def test_killers(self):
        for added in KILLERS:
            with self.subTest(added=added):
                self.assertTrue(self.found(added))

    def test_harmless(self):
        for added in HARMLESS:
            with self.subTest(added=added):
                self.assertIsNone(self.found(added))

    def test_only_python_and_only_added_lines(self):
        self.assertIsNone(self.found(["os._exit(3)"], path="docs/notes.md"))
        (fp,) = parse(patch_for(["    return 2"], removed="    os._exit(3)\n"))
        self.assertIsNone(impact._kills_process(fp))

    def test_flag(self):
        fps = parse(patch_for(["    os._exit(3)"]))
        with mock.patch.dict(os.environ, {"BR_CRASH_GUARD": "1"}):
            with self.assertRaises(impact.Everything) as caught:
                impact.commit_impact(fps, "/nonexistent", None)
            self.assertIn("process-killing", str(caught.exception))
        with mock.patch.dict(os.environ):
            os.environ.pop("BR_CRASH_GUARD", None)
            self.assertFalse(impact.crash_guard())
            self.assertIsNone(impact._kills_process(parse(patch_for(["    return 2"]))[0]))


if __name__ == "__main__":
    unittest.main()
