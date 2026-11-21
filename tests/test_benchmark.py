"""Tests for tools/benchmark.py: scoring arithmetic, score.py agreement, CPU measurement,
and one end-to-end run on a few sample patches.

    python3 -m unittest discover -s tests -t .
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("benchmark", ROOT / "tools" / "benchmark.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

SRC = Path(os.environ.get("BR_TEST_SRC", ROOT.parents[2] / "work" / "sqlglot"))


def scratch():
    (ROOT / "work").mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="test-bench-", dir=ROOT / "work"))


class Arithmetic(unittest.TestCase):
    ms = {0: 10_000, 1: 5_000, 2: 5_000, 3: 1, 4: 0, 5: 3_000, 6: 2_999}

    def test_duration_rounds_half_up(self):
        self.assertEqual(bench.duration_ms(0.0005), 1)
        self.assertEqual(bench.duration_ms(0.0015), 2)  # round() would give 2; 0.0025 differs
        self.assertEqual(bench.duration_ms(0.0025), 3)
        self.assertEqual(bench.duration_ms(21.454), 21_454)

    def test_cost(self):
        self.assertEqual(bench.cost_ms([], self.ms), 0)
        self.assertEqual(bench.cost_ms([[], []], self.ms), 0)
        self.assertEqual(bench.cost_ms([[0], []], self.ms), 12_000)
        self.assertEqual(bench.cost_ms([[0], [1, 2]], self.ms), 14_000)
        self.assertEqual(bench.cost_ms([[4]], self.ms), 2_000)

    def test_floor_by_hand(self):
        self.assertEqual(bench.floor_ms([], self.ms), 0)
        self.assertEqual(bench.floor_ms([3], self.ms), 2_001)
        # {10000, 5000, 5000}: k=1 22000, k=2 max(10000,10000)+4000 = 14000, k=3 16000.
        self.assertEqual(bench.floor_ms([0, 1, 2], self.ms), 14_000)
        # {5000, 3000, 2999} total 10999: k=1 12999, k=2 max(5000,5500)+4000 = 9500, k=3 11000.
        self.assertEqual(bench.floor_ms([1, 5, 6], self.ms), 9_500)
        # Zero-duration test: still one worker.
        self.assertEqual(bench.floor_ms([4], self.ms), 2_000)

    def test_floor_caps_at_eight_workers(self):
        ms = {i: 10_000 for i in range(20)}
        # k=8: max(10000, 25000) + 16000 = 41000; k=7: 28572+14000 = 42572; k=9 not allowed.
        self.assertEqual(bench.floor_ms(range(20), ms), 41_000)

    def test_split_against_floor(self):
        for failing in ([0, 1, 2], [3], []):
            self.assertEqual(bench.split_cost_ms(failing, self.ms), bench.floor_ms(failing, self.ms))
        # The bound is not always attainable: {5000, 3000, 2999} cannot be halved, the best
        # split is 5000 | 5999 -> 9999 > 9500. The benchmark reports such commits.
        self.assertEqual(bench.split_cost_ms([1, 5, 6], self.ms), 9_999)

    def test_score_commit_miss(self):
        s = bench.score_commit([[0], [1]], {0, 2}, self.ms)
        self.assertEqual(s["missed"], [2])
        self.assertEqual(s["cost_ms"], 14_000)
        self.assertEqual(s["charged_ms"], 314_000)

    def test_parse_config(self):
        self.assertEqual(bench.parse_config("a:X=1,Y=2"), ("a", {"X": "1", "Y": "2"}))
        self.assertEqual(bench.parse_config("plain:"), ("plain", {}))
        self.assertEqual(bench.parse_config("plain"), ("plain", {}))
        with self.assertRaises(Exception):
            bench.parse_config("X=1")
        with self.assertRaises(Exception):
            bench.parse_config("a:X")

    def test_unknown_excluded(self):
        d = scratch()
        try:
            p = d / "answers.json"
            p.write_text(json.dumps({"commits": {"c": {"failing": [1, 2], "unknown": [2], "kind": "k"}}}))
            a = bench.load_answers(p)
            self.assertEqual(a["c"]["failing"], {1})
            self.assertEqual(bench.answers_for_score_py(a),
                             {"commits": {"c": {"failing": [1], "kind": "k"}}})
        finally:
            shutil.rmtree(d)


class AgainstScorePy(unittest.TestCase):
    """Synthetic outputs scored both here and by score.py must agree exactly."""

    def setUp(self):
        self.dir = scratch()
        self.tests = [{"number": i, "id": f"t::{i}", "file": "t", "seconds": s}
                      for i, s in enumerate([1.5, 0.25, 0.0005, 3.0, 0.0, 2.0, 0.75, 0.125, 0.5, 1.0])]
        self.ms = {t["number"]: bench.duration_ms(t["seconds"]) for t in self.tests}
        (self.dir / "commits").mkdir()
        (self.dir / "tests.json").write_text(json.dumps({"tests": self.tests}))
        self.answers = {
            "a": {"failing": {0, 3}, "unknown": set(), "kind": "code"},
            "b": {"failing": set(), "unknown": set(), "kind": "code"},
            # As load_answers builds it: unknown 9 already removed from failing, so not a miss.
            "c": {"failing": {5}, "unknown": {9}, "kind": "code"},
            "d": {"failing": {1}, "unknown": set(), "kind": "code"},
        }
        for c in self.answers:
            (self.dir / "commits" / f"{c}.patch").write_text("")
        self.answers_path = self.dir / "answers.json"
        self.answers_path.write_text(json.dumps(bench.answers_for_score_py(self.answers)))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def check(self, outputs, cpu_us, expect_sim=None, expect_missed=None):
        path = self.dir / "out.jsonl"
        path.write_text("".join(json.dumps({"commit": c, "workers": w}) + "\n" for c, w in outputs.items()))
        rows = {c: bench.score_commit(outputs[c], self.answers[c]["failing"], self.ms) for c in outputs}
        sim = sum(r["charged_ms"] for r in rows.values())
        mine = {"simulated_ms": sim, "misses": sum(bool(r["missed"]) for r in rows.values()),
                "missed_commits": [c for c, r in rows.items() if r["missed"]],
                "cpu_us": cpu_us, "total_us": sim * 1000 + cpu_us}
        theirs = bench.run_score_py(self.dir, self.answers_path, path, cpu_us)
        bench.cross_check(mine, theirs, "synthetic")
        if expect_sim is not None:
            self.assertEqual(sim, expect_sim)
        if expect_missed is not None:
            self.assertEqual(mine["missed_commits"], expect_missed)
        return mine

    def test_miss_and_empty_workers(self):
        outputs = {
            "a": [[0], [], [3]],          # 3000 + 2*2000 = 7000
            "b": [[], []],                # 0
            "c": [[5]],                   # 2000 + 2000 = 4000, unknown 9 left out: no miss
            "d": [[2, 4]],                # 1 + 2000, misses test 1 -> +300000
        }
        self.check(outputs, 1_234_567, expect_sim=7000 + 4000 + 2001 + 300_000, expect_missed=["d"])

    def test_eight_workers(self):
        outputs = {
            "a": [[i] for i in (0, 1, 2, 3, 5, 6, 7, 8)],  # 8 workers: 3000 + 16000
            "b": [],
            "c": [[5, 9]],
            "d": [[1]],
        }
        self.check(outputs, 0, expect_sim=19_000 + 0 + 5_000 + 2_250)

    def test_rejection_is_loud(self):
        outputs = {"a": [[i] for i in range(9)], "b": [], "c": [[5]], "d": [[1]]}  # 9 workers
        with self.assertRaises(bench.CrossCheckError):
            self.check(outputs, 0)

    def test_disagreement_is_loud(self):
        outputs = {"a": [[0, 3]], "b": [], "c": [[5]], "d": [[1]]}
        mine = self.check(outputs, 10)
        theirs = dict(mine, simulated_ms=mine["simulated_ms"] + 1)
        with self.assertRaises(bench.CrossCheckError):
            bench.cross_check(mine, theirs, "x")


class Measurement(unittest.TestCase):
    def test_grandchild_cpu_is_counted(self):
        d = scratch()
        try:
            burn = "import time\nt=time.process_time()\nwhile time.process_time()-t<0.3: pass\n"
            parent = ("import subprocess, sys\n"
                      f"subprocess.run([sys.executable, '-c', {burn!r}])\n")
            m = bench.measure_process([sys.executable, "-c", parent], dict(os.environ), str(d),
                                      d / "o", d / "e", timeout=60)
            self.assertEqual(m["exit_code"], 0)
            self.assertGreaterEqual(m["cpu_us"], 280_000)
            self.assertEqual(m["cpu_us"], m["rusage_children_delta_us"])
            self.assertFalse(m["leftover_processes"])
        finally:
            shutil.rmtree(d)

    def test_orphan_is_reported(self):
        d = scratch()
        try:
            parent = ("import subprocess, sys\n"
                      "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n")
            m = bench.measure_process([sys.executable, "-c", parent], dict(os.environ), str(d),
                                      d / "o", d / "e", timeout=60)
            self.assertTrue(m["leftover_processes"])
        finally:
            shutil.rmtree(d)

    def test_parse_logs(self):
        text = ("[br] maps: 1 tests\n[br] a: every test (unparseable patch)\n"
                "[br] b: test_x passes on the patched code for ['x']; dropped\n[br] b: 3 tests; note\n")
        logs = bench.parse_logs(text, ["a", "b"])
        self.assertEqual(logs["every_test_commits"], ["a"])
        self.assertEqual(logs["replica_line_count"], 1)
        self.assertEqual(len(logs["per_commit"]["b"]), 2)


@unittest.skipUnless((SRC / "sqlglot").is_dir(), f"pinned sqlglot checkout not found at {SRC}")
class EndToEnd(unittest.TestCase):
    def test_sample_subset(self):
        d = scratch()
        try:
            patches = d / "patches"
            patches.mkdir()
            answers = json.loads((ROOT / "sample" / "answers.json").read_text())["commits"]
            pick = ["2haz", "2vdj", "9u5k"]
            for c in pick:
                shutil.copy(ROOT / "sample" / "commits" / f"{c}.patch", patches)
            (d / "answers.json").write_text(json.dumps({"commits": {c: answers[c] for c in pick}}))
            out = d / "result.json"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = bench.main([
                    "--patches", str(patches), "--tests", str(ROOT / "sample" / "tests.json"),
                    "--answers", str(d / "answers.json"), "--src", str(SRC), "--runs", "1",
                    "--config", "a:BR_ASSUME_TRUSTED=1,BR_NO_REPLICA=1", "--config", "b:BR_NO_REPLICA=1",
                    "--out", str(out), "--no-precompile",
                ])
            self.assertEqual(rc, 0)
            r = json.loads(out.read_text())
            self.assertEqual(r["floor"]["total_ms"], 2_083)
            a = r["configs"]["a"]
            self.assertEqual(a["runs"][0]["misses"], 0)
            self.assertTrue(a["runs"][0]["score_py_agrees"])
            self.assertEqual(a["runs"][0]["commits"]["2vdj"]["floor_ms"], 2_083)
            self.assertEqual(a["runs"][0]["commits"]["9u5k"]["cost_ms"], 0)
            self.assertIn("vs_first", r["configs"]["b"])
            self.assertGreater(a["runs"][0]["cpu_us"], 0)
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
