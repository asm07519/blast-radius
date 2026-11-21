"""Mode 0 admission (PRD §7, M1): verify a commit's cheap candidates by running them.

When the static selection S of a commit is small and cheap, run its Mode 0 eligible tests R
(in catalog order) on the patched code in ONE fresh child process (selector/mode0_runner.py,
in-memory overlay of the patched modules) and drop each test observed to pass. Everything
else stays selected. Off unless BR_MODE0=1; with the flag off nothing here runs.

Contract: which patches may be verified at all (`contract`)
----------------------------------------------------------
The in-memory overlay serves the patched modules' new bytes to every import inside the child
process. That is a faithful stand-in for the patched checkout only when the patch is nothing
but edits to existing, importable sqlglot modules. Admitted, all of these at once:

  * impact.commit_impact left `res.replica_ok` true. For each file, `_modified` keeps it true
    only for an existing `sqlglot/**.py` file whose patch applies, that parses, has no docstring
    site, and that no mapped test reads as text (maps.reads, including linecache reads);
  * `res.patched` is non-empty and holds exactly the paths of the patch's file entries, each
    path once (a path patched twice would be overlaid with only its last entry applied to the
    pinned source, so it is refused);
  * there is at least one call-time site or import-time file (`res.call_sites` /
    `res.import_files`). A patch whose only effect is an is/== swap that the type profile
    proved a no-op, or that has no semantic change, has nothing to verify;
  * every patched path is a plain module `sqlglot/<pkg>/.../<name>.py`: identifier path
    parts, a regular file (no symlink) inside a chain of regular packages (each directory
    holds `__init__.py`, none is a symlink), not a package `__init__.py`, not shadowed by a
    package directory `<name>/__init__.py`, and with no compiled extension next to it
    (`<name>` + any importlib.machinery.EXTENSION_SUFFIXES, e.g. a mypyc build from
    `sqlglotc/`): the import system prefers an extension and a package over `<name>.py`, so
    the real run would not import the patched source.

Never admitted (impact.py already clears replica_ok or raises Everything, or the checks above
refuse): added, deleted, renamed or copied files; binary patches; mode-only entries (they are
not in `res.patched`); data/fixture files and any other non-.py file (docs included); test
files, conftest.py, top-level modules and anything outside `sqlglot/`; pytest configs;
`sqlglotc/`; a patch that does not apply, does not parse or changes the coding cookie (syntax
sites); docstring edits; files some mapped test reads as text; package `__init__.py` edits
(excluded conservatively: the overlay does set up the package, but such edits reach every
importer and the gate would reject them anyway); every "every test" fallback (Everything,
errors, budget overruns, unparseable or empty patches: Mode 0 is never reached).

What the contract cannot see statically is caught at run time by the runner and makes the test
"flagged"/"error", which keeps it: a test that spawns a process (a child would import unpatched
modules from disk), opens a patched module's file (inspect.getsource, a path-based loader),
displaces the overlay finder or leaves the cwd. This is what makes untrusted or unmapped tests
safe to run too: their reads are not in the maps, so replica_ok cannot account for them, but
the runner's audit hook observes every open of a patched path.

Eligibility (`load_eligibility`, file from tools/mode0_eligibility.py)
----------------------------------------------------------------------
Loaded lazily, once. The whole file is refused (Mode 0 off for the run, one log line) when the
schema is unknown, runner digest, tests identity or source digest differ, the Python
major.minor differs from this interpreter, the configured engine (or, for unittest, pytest)
was not certified, the file has no order (batch) or seed certification, or it does not parse.
A test (looked up by id, never by number) is eligible when, for every certified engine, every
certification run (production launch and each hash seed) passed with no flag other than the
informational "rtracker", the batch run passed, and it is neither order sensitive nor an
engine disagreement. With the in-memory overlay a test certified with "spawn" is never
eligible (the flag is unsafe in any case).

Expected-value gate (`gate`, PRD §7)
------------------------------------
R = eligible tests of S. Admit iff |R| <= MAX_TESTS, recorded seconds of R <= MAX_SECONDS and
    P * delta - cpu >= MIN_GAIN   and   P * delta >= RATIO * cpu
where delta = cost(pack(S)) - cost(pack(S - R)) (schedule.split/cost, integer ms) and
cpu = CPU_FACTOR * (max over R of (proc - cpu_t) + sum over R of cpu_t), from the single-test
certification runs (cpu_t: the test's own CPU, proc: its whole process). P is a fixed prior,
not a function of the patch. Then budgets: the run-wide Mode 0 CPU spent so far plus this
estimate must fit CPU_BUDGET, and the wall left before the replay budget (and before the hard
budget minus RESERVE) must cover the estimate plus MARGIN. The child's timeout is
STARTUP_CAP + sum of per-test caps + MARGIN, cut to that remaining wall; per-test cap =
max(CAP_MIN, CAP_MULT * seconds + CAP_ADD). Exceeding any cap keeps the tests.

Using the result: a test leaves S only if execute() reports exactly "passed" for it, the
child exited 0 without timing out and its line has no unsafe flag. Anything else, and any
exception, leaves S exactly as it was. The child CPU of every attempt counts towards
CPU_BUDGET. stdout is never written to.
"""

import importlib.machinery
import json
import math
import os
import sys
import time

from . import mode0_runner as m0
from . import schedule
from .replica import overlay_spec

SCHEMA = "br-mode0-eligibility/1"
ENGINES = ("unittest", "pytest")
OVERLAYS = ("memory",)
# Flags a certification pass may carry and still count (the resource tracker runs only stdlib).
BENIGN_FLAGS = frozenset({"rtracker"})

# name -> (env var, default, lower bound, upper bound)
TUNABLES = {
    "max_tests": ("BR_MODE0_MAX_TESTS", 40, 0, 10_000),
    "max_seconds": ("BR_MODE0_MAX_SECONDS", 1.5, 0, 1e4),
    "cpu_factor": ("BR_MODE0_CPU_FACTOR", 1.5, 0, 1e3),
    "p": ("BR_MODE0_P", 0.5, 0, 1),
    "min_gain": ("BR_MODE0_MIN_GAIN", 0.5, -1e4, 1e4),
    "ratio": ("BR_MODE0_RATIO", 1.5, 0, 1e4),
    "cpu_budget": ("BR_MODE0_CPU_BUDGET", 30.0, 0, 1e5),
    "cap_min": ("BR_MODE0_CAP_MIN", 2.0, 0.1, 600),
    "cap_mult": ("BR_MODE0_CAP_MULT", 10.0, 0, 1e3),
    "cap_add": ("BR_MODE0_CAP_ADD", 1.0, 0, 600),
    "startup_cap": ("BR_MODE0_STARTUP_CAP", 10.0, 0.1, 600),
    "margin": ("BR_MODE0_MARGIN", 2.0, 0, 600),
    "reserve": ("BR_MODE0_RESERVE", 10.0, 0, 600),
}


class Refused(Exception):
    """The eligibility file cannot be used for this run."""


class Config:
    def __init__(self, env=None, app=None):
        env = os.environ if env is None else env
        for name, (var, default, lo, hi) in TUNABLES.items():
            raw = env.get(var)
            value = default if raw is None or raw == "" else float(raw)
            if not (math.isfinite(value) and lo <= value <= hi):
                raise ValueError(f"{var}={raw!r} out of range")
            setattr(self, name, value)
        self.max_tests = int(self.max_tests)
        self.engine = env.get("BR_MODE0_ENGINE") or "unittest"
        if self.engine not in ENGINES:
            raise ValueError(f"BR_MODE0_ENGINE={self.engine!r}")
        app = app or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.elig_path = env.get("BR_MODE0_ELIG") or os.path.join(app, "maps", "mode0.json")
        self.site_env = env.get("BR_MODE0_SITE")

    def cap(self, seconds):
        return max(self.cap_min, self.cap_mult * seconds + self.cap_add)


# ------------------------------------------------------------------------------ contract

def _module_problem(path, src_root):
    """None if the overlay serves `path` exactly where the import system would load it."""
    if not (path.startswith("sqlglot/") and path.endswith(".py")):
        return "not a sqlglot module"
    parts = path[:-3].split("/")
    if not all(p.isidentifier() for p in parts):
        return "path is not a module name"
    if parts[-1] == "__init__":
        return "package __init__"
    full = os.path.join(src_root, path)
    if os.path.islink(full) or not os.path.isfile(full):
        return "not a regular file"
    for k in range(1, len(parts)):
        d = os.path.join(src_root, *parts[:k])
        if os.path.islink(d) or not os.path.isfile(os.path.join(d, "__init__.py")):
            return "not inside a chain of regular packages"
    stem = os.path.join(src_root, *parts)
    if os.path.exists(os.path.join(stem, "__init__.py")):
        return "shadowed by a package"
    for suffix in importlib.machinery.EXTENSION_SUFFIXES:
        if os.path.exists(stem + suffix):
            return "compiled extension next to it"
    return None


def contract(fps, res, src_root):
    """None when the in-memory overlay represents the patch faithfully, else the reason."""
    if not res.replica_ok:
        return "not an overlay-able patch"
    if not res.patched:
        return "nothing patched"
    if not (res.call_sites or res.import_files):
        return "no call/import site"
    paths = []
    for fp in fps:
        if fp.mode_only:
            return "mode-only entry"
        if fp.binary or getattr(fp, "copy", False):
            return "binary/copy entry"
        if fp.old_path is None or fp.new_path is None or fp.old_path != fp.new_path:
            return "added/deleted/renamed file"
        paths.append(fp.old_path)
    if len(set(paths)) != len(paths):
        return "a file is patched twice"
    if set(paths) != set(res.patched):
        return "patched modules do not cover every changed file"
    for path in paths:
        problem = _module_problem(path, src_root)
        if problem:
            return f"{path}: {problem}"
    return None


# --------------------------------------------------------------------------- eligibility

def _number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


def _engine_ok(info, run_names, overlay):
    if not isinstance(info, dict) or info.get("ok") is not True or info.get("why") is not None:
        return False
    flags = info.get("flags")
    if not isinstance(flags, list) or not set(flags) <= BENIGN_FLAGS:
        return False
    if overlay == "memory" and "spawn" in flags:
        return False
    outs = info.get("o")
    if not isinstance(outs, dict) or any(outs.get(name) != "passed" for name in run_names):
        return False
    return all(o == "passed" for o in outs.values())


def eligible_entry(entry, engine, certified, run_names, overlay="memory"):
    """(cpu, proc) of a test eligible for `engine`, or None."""
    if not isinstance(entry, dict) or entry.get("order_sensitive") or entry.get("disagree"):
        return None
    batch = entry.get("batch")
    if not isinstance(batch, dict):
        return None
    for e in certified:
        if not _engine_ok(entry.get(e), run_names, overlay) or batch.get(e) != "passed":
            return None
    info = entry[engine]
    cpu, proc = info.get("cpu"), info.get("proc")
    if not (_number(cpu) and _number(proc)):
        return None
    return float(cpu), float(proc)


def load_eligibility(path, tests, src_root, engine, overlay="memory", python=None):
    """{test id: (cpu, proc)} of the tests eligible for `engine`; raises Refused."""
    python = tuple(python or sys.version_info[:2])
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"), parse_constant=m0._strict_constant)
    except FileNotFoundError:
        raise Refused(f"no eligibility file {path}") from None
    except Exception as error:
        raise Refused(f"eligibility file does not parse ({type(error).__name__})") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise Refused("unknown eligibility schema")
    header, entries = data.get("header"), data.get("tests")
    if not isinstance(header, dict) or not isinstance(entries, dict):
        raise Refused("eligibility file has no header/tests")
    version = header.get("python")
    try:
        file_python = tuple(int(x) for x in str(version).split(".")[:2])
    except ValueError:
        file_python = None
    if file_python != python:
        raise Refused(f"python {version} != {python[0]}.{python[1]}")
    impl = header.get("implementation")
    if impl is not None and impl != sys.implementation.name:
        raise Refused(f"implementation {impl}")
    if header.get("runner_digest") != m0.runner_digest():
        raise Refused("runner digest differs")
    if header.get("tests_identity") != m0.tests_identity(tests):
        raise Refused("tests identity differs")
    if header.get("source_digest") != m0.source_digest(src_root):
        raise Refused("source digest differs")
    engines = header.get("engines")
    certified = [e for e in ENGINES if isinstance(engines, dict) and e in engines]
    if engine not in certified or (engine == "unittest" and "pytest" not in certified):
        raise Refused(f"engine {engine} not certified (needs pytest agreement for unittest)")
    runs = header.get("runs")
    run_names = [r.get("name") for r in runs] if isinstance(runs, list) and all(isinstance(r, dict) for r in runs) else []
    if "iso" not in run_names or len(run_names) < 2 or not all(isinstance(n, str) for n in run_names):
        raise Refused("no seed certification")
    if header.get("also_batch") is not True:
        raise Refused("no order (batch) certification")
    out = {}
    for t in tests:
        got = eligible_entry(entries.get(t["id"]), engine, certified, run_names, overlay)
        if got is not None:
            out[t["id"]] = got
    return out


# ----------------------------------------------------------------------------- the gate

class Decision:
    def __init__(self, admit, why, n=0, n_s=0, secs=0.0, delta=0, cpu=0.0, benefit=0.0):
        self.admit, self.why = admit, why
        self.n, self.n_s, self.secs, self.delta, self.cpu, self.benefit = n, n_s, secs, delta, cpu, benefit

    def numbers(self):
        return (f"R={self.n}/{self.n_s} s={self.secs:.3f} d={self.delta}ms "
                f"P*d={self.benefit:.3f} cpu~{self.cpu:.3f} net={self.benefit - self.cpu:+.3f}")


EPS = 1e-9


def gate(selected, R, ms, info, cfg):
    """Expected-value admission for running R (eligible part of `selected`); info: id-free
    list of (cpu, proc) for R."""
    n, n_s = len(R), len(selected)
    total_ms = sum(ms[t] for t in R)
    secs = total_ms / 1000
    if not R:
        return Decision(False, "no eligible candidate", n, n_s)
    if n > cfg.max_tests:
        return Decision(False, "too many tests", n, n_s, secs)
    if total_ms > round(cfg.max_seconds * 1000):
        return Decision(False, "too many recorded seconds", n, n_s, secs)
    rest = set(selected) - set(R)
    delta = schedule.cost(schedule.split(selected, ms), ms) - schedule.cost(schedule.split(rest, ms), ms)
    startup = max(max(proc - cpu, 0.0) for cpu, proc in info)
    cpu = cfg.cpu_factor * (startup + sum(c for c, _ in info))
    benefit = cfg.p * delta / 1000
    d = Decision(False, "", n, n_s, secs, delta, cpu, benefit)
    if benefit - cpu < cfg.min_gain - EPS:
        d.why = "gain below minimum"
    elif benefit < cfg.ratio * cpu - EPS:
        d.why = "gain/cpu ratio below minimum"
    else:
        d.admit, d.why = True, "admitted"
    return d


# ------------------------------------------------------------------------ site-packages

def site_dirs(site_env=None):
    """Existing site-packages dirs for the runner (it starts with -I -S, so it has none)."""
    if site_env:
        cands = [d for d in site_env.split(os.pathsep) if d]
    else:
        import sysconfig

        cands = []
        # Under -S, sys.prefix is the base interpreter even inside a venv: find pyvenv.cfg.
        venv = os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
        cfg = os.path.join(venv, "pyvenv.cfg")
        system = True
        if os.path.isfile(cfg):
            system = False
            try:
                with open(cfg, encoding="utf-8") as fh:
                    for line in fh:
                        key, _, value = line.partition("=")
                        if key.strip() == "include-system-site-packages":
                            system = value.strip().lower() == "true"
            except OSError:
                pass
            # The "venv" scheme (3.11+) ignores distributor tweaks of the default scheme
            # (e.g. Homebrew's framework layout, Debian's dist-packages).
            names = sysconfig.get_scheme_names()
            scheme = "venv" if "venv" in names else ("nt" if os.name == "nt" else "posix_prefix")
            paths = sysconfig.get_paths(scheme=scheme, vars={"base": venv, "platbase": venv})
            cands += [paths.get("purelib"), paths.get("platlib")]
        if system:
            paths = sysconfig.get_paths()
            cands += [paths.get("purelib"), paths.get("platlib")]
    out = []
    for d in cands:
        if d and os.path.isdir(d):
            d = os.path.realpath(d)
            if d not in out:
                out.append(d)
    return out


# ------------------------------------------------------------------------------ session

class Session:
    """Run-wide Mode 0 state: config, lazily loaded eligibility, CPU spent, wall limits.

    Nothing per commit is kept between verify() calls."""

    def __init__(self, cfg, tests, src_root, started, wall_budget, hard_budget, log,
                 execute=None, clock=time.monotonic, python=None):
        self.cfg = cfg
        self.tests = tests
        self.src_root = os.path.realpath(src_root)
        self.started, self.wall_budget, self.hard_budget = started, wall_budget, hard_budget
        self.log = log
        self.execute = execute or m0.execute
        self.clock = clock
        self.python = python
        self.order = {t["number"]: k for k, t in enumerate(tests)}
        self.ids = {t["number"]: t["id"] for t in tests}
        self.seconds = {t["number"]: float(t["seconds"]) for t in tests}
        self.spent = 0.0
        self.off = None  # reason once Mode 0 is off for the rest of the run
        self._elig = None
        self.site = site_dirs(cfg.site_env)
        if not self.site:
            self._disable("no site-packages directory")

    def _disable(self, reason):
        if self.off is None:
            self.off = reason
            self.log(f"mode0 off: {reason}")

    def eligibility(self):
        if self._elig is None and self.off is None:
            try:
                self._elig = load_eligibility(self.cfg.elig_path, self.tests, self.src_root,
                                              self.cfg.engine, python=self.python)
                self.log(f"mode0: {len(self._elig)} {self.cfg.engine}-eligible tests")
            except Refused as why:
                self._disable(str(why))
        return self._elig

    def verify(self, cid, selected, res, fps, ms, overlay="memory"):
        """The selection after Mode 0: `selected` minus tests observed to pass. Never raises;
        on any problem returns `selected` unchanged."""
        try:
            return self._verify(cid, selected, res, fps, ms, overlay)
        except Exception as error:  # noqa: BLE001 - Mode 0 must never change the fallback path
            self.log(f"{cid}: mode0 error {error!r}; kept")
            return selected

    def _verify(self, cid, selected, res, fps, ms, overlay):
        if self.off is not None or not selected:
            return selected
        if overlay not in OVERLAYS:
            self.log(f"{cid}: mode0 skip (overlay {overlay!r} not supported)")
            return selected
        why = contract(fps, res, self.src_root)
        if why:
            self.log(f"{cid}: mode0 skip ({why})")
            return selected
        elig = self.eligibility()
        if elig is None:
            return selected
        selected = set(selected)
        R = [n for n in sorted(selected, key=lambda n: self.order.get(n, n)) if self.ids.get(n) in elig]
        d = gate(selected, R, ms, [elig[self.ids[n]] for n in R], self.cfg)
        if not d.admit:
            self.log(f"{cid}: mode0 reject ({d.why}) {d.numbers()}")
            return selected
        cfg = self.cfg
        if self.spent + d.cpu > cfg.cpu_budget + EPS:
            if self.spent >= cfg.cpu_budget:
                self._disable(f"cpu budget spent ({self.spent:.2f}s)")
            self.log(f"{cid}: mode0 reject (cpu budget: spent {self.spent:.2f}s) {d.numbers()}")
            return selected
        now = self.clock()
        avail = min(self.started + self.wall_budget, self.started + self.hard_budget - cfg.reserve) - now
        if avail < d.cpu + cfg.margin:
            self.log(f"{cid}: mode0 reject (wall budget: {avail:.1f}s left) {d.numbers()}")
            return selected
        caps = [round(cfg.cap(self.seconds[n]), 3) for n in R]
        timeout = min(cfg.startup_cap + sum(caps) + cfg.margin, avail)
        spec = {"kind": "mode0", "src": self.src_root,
                "patched": overlay_spec(res.patched, self.src_root),
                "engine": cfg.engine, "site": list(self.site), "startup_cap": cfg.startup_cap,
                "tests": [{"id": self.ids[n], "cap": cap} for n, cap in zip(R, caps)]}
        try:
            out = self.execute(spec, timeout)
        except Exception as error:  # noqa: BLE001
            self.spent += d.cpu  # unknown: charge the estimate
            self.log(f"{cid}: mode0 run failed {error!r}; kept {d.numbers()}")
            return selected
        cpu = out.get("cpu", 0.0) if isinstance(out, dict) else None
        if not _number(cpu):
            cpu = d.cpu  # unknown: charge the estimate
        self.spent += cpu
        passed = _passed(out, [self.ids[n] for n in R])
        dropped = {n for n in R if self.ids[n] in passed}
        kept = selected - dropped
        counts = {}
        outcomes = out.get("outcomes") if isinstance(out, dict) else None
        for o in outcomes.values() if isinstance(outcomes, dict) else ():
            counts[str(o)] = counts.get(str(o), 0) + 1
        info = out if isinstance(out, dict) else {}
        wall = info.get("wall") if _number(info.get("wall")) else 0.0
        self.log(f"{cid}: mode0 admit {d.numbers()} -> dropped {len(dropped)}/{len(R)} "
                 f"{dict(sorted(counts.items()))} rc={info.get('returncode')} "
                 f"timeout={info.get('timed_out')} cpu={cpu:.3f}s wall={wall:.2f}s spent={self.spent:.2f}s")
        if self.spent >= cfg.cpu_budget:
            self._disable(f"cpu budget spent ({self.spent:.2f}s)")
        return kept


def _passed(out, ids):
    """Ids with exactly "passed" from a clean, complete, unflagged run."""
    if not isinstance(out, dict) or out.get("returncode") != 0 or out.get("timed_out") is not False:
        return set()
    outcomes = out.get("outcomes")
    if not isinstance(outcomes, dict):
        return set()
    lines = out.get("lines") if isinstance(out.get("lines"), dict) else {}
    ok = set()
    for tid in ids:
        if outcomes.get(tid) != "passed":
            continue
        line = lines.get(tid)
        if not isinstance(line, dict) or line.get("outcome") != "passed":
            continue
        flags = line.get("flags")
        if not isinstance(flags, list) or m0.UNSAFE_FLAGS.intersection(flags):
            continue
        ok.add(tid)
    return ok


def from_env(tests, src_root, started, wall_budget, hard_budget, log, env=None):
    """A Session when BR_MODE0=1 (else None). Bad settings turn Mode 0 off with one log line."""
    env = os.environ if env is None else env
    if env.get("BR_MODE0") != "1":
        return None
    try:
        cfg = Config(env)
    except ValueError as error:
        log(f"mode0 off: {error}")
        return None
    session = Session(cfg, tests, src_root, started, wall_budget, hard_budget, log)
    return None if session.off else session
