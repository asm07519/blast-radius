"""Tests for BR_MUTATION_GUARD and BR_HANG_GUARD (selector/guards.py)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selector import guards, impact
from selector.patch import parse


def _make_patch(path, old_line, new_lines):
    body = "".join(f"+{line}\n" for line in new_lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,4 +1,{3 + len(new_lines)} @@\n"
        f" class Foo:\n"
        f"     arg_types = {{'a': True}}\n"
        f"     def f(self):\n"
        f"-{old_line}\n"
        f"{body}"
    ).encode()


class MutationGuardTest(unittest.TestCase):
    def test_detects_class_attr_pop_via_self(self):
        old = b"class Foo:\n    arg_types = {'a': True}\n    def f(self):\n        return 1\n"
        new = b"class Foo:\n    arg_types = {'a': True}\n    def f(self):\n        self.arg_types.pop('a', None)\n        return 1\n"
        self.assertIsNotNone(guards.added_shared_mutation(old, new))

    def test_detects_inherited_uppercase_class_attr_discard(self):
        old = b"class Sub(Base):\n    def f(self):\n        return 1\n"
        new = b"class Sub(Base):\n    def f(self):\n        self.RESERVED_KEYWORDS.discard('x')\n        return 1\n"
        self.assertIsNotNone(guards.added_shared_mutation(old, new))

    def test_detects_module_global_mutation(self):
        old = b"TABLE = {'k': 1}\ndef f():\n    return 1\n"
        new = b"TABLE = {'k': 1}\ndef f():\n    TABLE.pop('k', None)\n    return 1\n"
        self.assertIsNotNone(guards.added_shared_mutation(old, new))

    def test_detects_global_counter_subscript(self):
        old = b"def f():\n    return 1\n"
        new = b"_CALLS = [0]\ndef f():\n    _CALLS[0] += 1\n    return 1\n"
        self.assertIsNotNone(guards.added_shared_mutation(old, new))

    def test_detects_global_rebind(self):
        old = b"X = 0\ndef f():\n    return X\n"
        new = b"X = 0\ndef f():\n    global X\n    X = 1\n    return X\n"
        self.assertIsNotNone(guards.added_shared_mutation(old, new))

    def test_ignores_local_and_instance_mutations(self):
        old = b"class Foo:\n    def f(self):\n        return 1\n"
        new = (
            b"class Foo:\n"
            b"    def f(self):\n"
            b"        xs = []\n"
            b"        xs.append(1)\n"
            b"        self.args.pop('k', None)\n"
            b"        self.x = 2\n"
            b"        return 1\n"
        )
        self.assertIsNone(guards.added_shared_mutation(old, new))

    def test_ignores_existing_shared_mutation(self):
        old = b"TABLE = {}\ndef f():\n    TABLE['a'] = 1\n    return 1\n"
        new = b"TABLE = {}\ndef f():\n    TABLE['a'] = 1\n    return 2\n"
        self.assertIsNone(guards.added_shared_mutation(old, new))

    def test_flag_default_off_and_raises_everything_when_on(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sqlglot" / "x.py"
            p.parent.mkdir()
            p.write_bytes(b"class Foo:\n    arg_types = {'a': True}\n    def f(self):\n        return 1\n")
            fps = parse(_make_patch("sqlglot/x.py", "        return 1",
                                    ["        self.arg_types.pop('a', None)", "        return 1"]))
            with mock.patch.dict(os.environ, {"BR_MUTATION_GUARD": "1"}):
                with self.assertRaises(impact.Everything) as caught:
                    impact.commit_impact(fps, td, None)
                self.assertIn("shared-state mutation", str(caught.exception))
            with mock.patch.dict(os.environ):
                os.environ.pop("BR_MUTATION_GUARD", None)
                self.assertFalse(impact.mutation_guard())


class HangGuardTest(unittest.TestCase):
    def test_detects_while_true_without_exit(self):
        old = b"def f():\n    return 1\n"
        new = b"def f():\n    while True:\n        pass\n    return 1\n"
        self.assertEqual(guards.added_endless_loop(old, new), "while True")

    def test_detects_iter_sentinel_without_exit(self):
        old = b"def f():\n    return 1\n"
        new = b"def f():\n    for _ in iter(int, 1):\n        pass\n    return 1\n"
        self.assertEqual(guards.added_endless_loop(old, new), "for ... in iter(..., ...)")

    def test_ignores_while_true_with_break_or_return_or_yield(self):
        old = b"def f():\n    return 1\n"
        for body in ("        break", "        return 1", "        raise ValueError()", "        yield 1"):
            new = f"def f():\n    while True:\n{body}\n".encode()
            with self.subTest(body=body):
                self.assertIsNone(guards.added_endless_loop(old, new))

    def test_nested_break_does_not_exit_outer_loop(self):
        old = b"def f():\n    return 1\n"
        new = b"def f():\n    while True:\n        for _ in range(3):\n            break\n"
        self.assertEqual(guards.added_endless_loop(old, new), "while True")

    def test_flag_default_off_and_raises_everything_when_on(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sqlglot" / "x.py"
            p.parent.mkdir()
            p.write_bytes(b"class Foo:\n    arg_types = {'a': True}\n    def f(self):\n        return 1\n")
            fps = parse(_make_patch("sqlglot/x.py", "        return 1",
                                    ["        while True:", "            pass", "        return 1"]))
            with mock.patch.dict(os.environ, {"BR_HANG_GUARD": "1"}):
                with self.assertRaises(impact.Everything) as caught:
                    impact.commit_impact(fps, td, None)
                self.assertIn("endless loop", str(caught.exception))
            with mock.patch.dict(os.environ):
                os.environ.pop("BR_HANG_GUARD", None)
                self.assertFalse(impact.hang_guard())


if __name__ == "__main__":
    unittest.main()
