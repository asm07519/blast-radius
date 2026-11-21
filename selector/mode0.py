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
major.minor (or implementation, or recorded machine architecture) differs from this
interpreter, the configured engine (or, for unittest, pytest)
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

import hashlib
import importlib.machinery
import json
import math
import os
import sys
import tempfile
import time

from . import mode0_runner as m0
from . import overlay as ovl
from . import schedule
from .replica import overlay_spec

SCHEMA = "br-mode0-eligibility/1"
ENGINES = ("unittest", "pytest")
OVERLAYS = ("memory",)
# Flags a certification pass may carry and still count (the resource tracker runs only stdlib).
BENIGN_FLAGS = frozenset({"rtracker"})
# In fs mode, also the audited multiprocessing children (mode0_runner: verified at creation).
FS_BENIGN_FLAGS = BENIGN_FLAGS | {"mp_spawn", "mp_fork"}
# BR_MODE0_WARM=1: default CPU_FACTOR of the warm-batch estimate (see `estimate`).
WARM_FACTOR = 1.3
MAX_SEED = 2 ** 32 - 1  # PYTHONHASHSEED range

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
    "fs_cpus": ("BR_MODE0_FS_CPUS", 2, 1, 1024),
}
# Switches, each "1" = on. Defaults off except EXACT (a contract fix; "0" turns it off for A/B).
SWITCHES = {
    "fs": ("BR_MODE0_FS", False),          # filesystem overlay for spawn-certified candidates
    "warm": ("BR_MODE0_WARM", False),      # warm-batch CPU estimate (eligibility "bcpu")
    "nosite": ("BR_MODE0_NOSITE", False),  # admit patches whose only sites were proven no-ops
    "exact": ("BR_MODE0_EXACT", True),     # every hunk at its stated position, exact bytes
}


class Refused(Exception):
    """The eligibility file cannot be used for this run."""


def parse_seeds(raw):
    """BR_MODE0_SEEDS: "" -> [] (one production run, random hash seed), "0,1" -> [0, 1]."""
    if raw is None or not raw.strip():
        return []
    seeds = []
    for part in raw.split(","):
        part = part.strip()
        if not part.isdigit() or int(part) > MAX_SEED:
            raise ValueError(f"BR_MODE0_SEEDS={raw!r}")
        if int(part) in seeds:
            raise ValueError(f"BR_MODE0_SEEDS={raw!r}: repeated seed")
        seeds.append(int(part))
    return seeds


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
        if self.fs_cpus != int(self.fs_cpus):
            raise ValueError(f"BR_MODE0_FS_CPUS={env.get('BR_MODE0_FS_CPUS')!r}")
        self.fs_cpus = int(self.fs_cpus)
        for name, (var, default) in SWITCHES.items():
            raw = env.get(var)
            if raw in (None, ""):
                value = default
            elif raw in ("0", "1"):
                value = raw == "1"
            else:
                raise ValueError(f"{var}={raw!r}")
            setattr(self, name, value)
        # The legacy estimate keeps its factor; the warm one (used only when every test of the
        # set has a warm number) gets WARM_FACTOR unless BR_MODE0_CPU_FACTOR is set.
        self.legacy_factor = self.cpu_factor
        if self.warm and env.get("BR_MODE0_CPU_FACTOR") in (None, ""):
            self.cpu_factor = WARM_FACTOR
        self.seeds = parse_seeds(env.get("BR_MODE0_SEEDS"))
        self.engine = env.get("BR_MODE0_ENGINE") or "unittest"
        if self.engine not in ENGINES:
            raise ValueError(f"BR_MODE0_ENGINE={self.engine!r}")
        app = app or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.elig_path = env.get("BR_MODE0_ELIG") or os.path.join(app, "maps", "mode0.json")
        self.site_env = env.get("BR_MODE0_SITE")

    @property
    def runs(self):
        """Hash seeds of the runs per attempt; [None] = one production (random seed) run."""
        return list(self.seeds) or [None]

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


def contract(fps, res, src_root, allow_no_site=False):
    """None when the in-memory overlay represents the patch faithfully, else the reason.

    allow_no_site (BR_MODE0_NOSITE=1): also admit a patch with no call-time site and no
    import-time file, i.e. whose every site was an is/== swap proven a no-op by the type
    profile (the selection is then the importers whose profile is incomplete, plus untrusted
    and unmapped tests). Everything else below still holds, in particular res.patched has the
    complete new source of every changed file."""
    if not res.replica_ok:
        return "not an overlay-able patch"
    if not res.patched:
        return "nothing patched"
    if not (res.call_sites or res.import_files) and not allow_no_site:
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


def _lines(data):
    """Split on b"\\n" only (as git does), keeping the line ends."""
    parts = data.split(b"\n")
    out = [p + b"\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def exact_apply(fp, old):
    """The new bytes when every hunk of `fp` applies to `old` exactly where it says, else None.

    Stricter than patch.apply (which searches +-200 lines and, last, ignores line endings) and
    than `git apply` (which also accepts offsets): each hunk's preimage must equal the pinned
    lines at old_start byte for byte, hunks must be in order and not overlap, line counts must
    match the @@ header, a hunk without trailing context must end at end of file (git's
    match_end), and only the last line of the result may lack a newline. Pure insertions
    (old_len 0, no context: git positions them differently from patch.apply) and any CR byte
    in a hunk are refused."""
    src = _lines(old)
    out, pos = [], 0
    for h in fp.hunks:
        if any(b"\r" in c for _, c in h.lines):
            return None
        pre = [c for t, c in h.lines if t != b"+"]
        post = [c for t, c in h.lines if t != b"-"]
        if h.old_len == 0 or len(pre) != h.old_len or len(post) != h.new_len:
            return None
        start = h.old_start - 1
        if start < pos or start + len(pre) > len(src) or src[start:start + len(pre)] != pre:
            return None
        if h.lines[-1][0] != b" " and start + len(pre) != len(src):
            return None
        out.extend(src[pos:start])
        out.extend(post)
        pos = start + len(pre)
    out.extend(src[pos:])
    if any(not line.endswith(b"\n") for line in out[:-1]):
        return None
    return b"".join(out)


def exact_problem(fps, res, src_root):
    """None when every file entry of the patch applies exactly (exact_apply) to the pinned
    bytes and gives exactly the bytes Mode 0 overlays (res.patched), else the reason.
    Assumes contract() passed (one plain modification per path, all in res.patched)."""
    for fp in fps:
        path = fp.old_path
        if fp.mode_only or fp.binary or not fp.hunks:
            return f"{path}: not a text edit"
        try:
            with open(os.path.join(src_root, path), "rb") as fh:
                old = fh.read()
        except OSError:
            return f"{path}: cannot read the pinned file"
        if b"\r" in old:
            return f"{path}: pinned file has CR bytes"
        new = exact_apply(fp, old)
        if new is None:
            return f"{path}: a hunk does not apply exactly at its stated position"
        if new == old:
            return f"{path}: file unchanged"
        if res.patched.get(path) != new:
            return f"{path}: overlaid bytes differ from the exact application"
    return None


# --------------------------------------------------------------------------- eligibility

def _number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


def _engine_ok(info, run_names, overlay):
    if not isinstance(info, dict) or info.get("ok") is not True or info.get("why") is not None:
        return False
    flags = info.get("flags")
    benign = FS_BENIGN_FLAGS if overlay == "fs" else BENIGN_FLAGS
    if not isinstance(flags, list) or not set(flags) <= benign:
        return False
    if overlay == "memory" and "spawn" in flags:
        return False
    outs = info.get("o")
    if not isinstance(outs, dict) or any(outs.get(name) != "passed" for name in run_names):
        return False
    return all(o == "passed" for o in outs.values())


def eligible_entry(entry, engine, certified, run_names, overlay="memory"):
    """(cpu, proc) of a test eligible for `engine` in `overlay` mode, or None.

    fs mode: the verdicts are the entry's "fs" block (per engine, same runs), which may carry
    the audited multiprocessing flags; order (batch) and engine agreement are certified in
    memory mode and must hold too."""
    if not isinstance(entry, dict) or entry.get("order_sensitive") or entry.get("disagree"):
        return None
    batch = entry.get("batch")
    if not isinstance(batch, dict):
        return None
    block = entry
    if overlay == "fs":
        block = entry.get("fs")
        if not isinstance(block, dict) or block.get("order_sensitive") or block.get("disagree"):
            return None
    for e in certified:
        if not _engine_ok(block.get(e), run_names, overlay) or batch.get(e) != "passed":
            return None
    info = block[engine]
    cpu, proc = info.get("cpu"), info.get("proc")
    if not (_number(cpu) and _number(proc)):
        return None
    return float(cpu), float(proc)


class Eligibility:
    """What Session needs from an eligibility file, for one engine.

    memory / fs  {id: (cpu, proc)} of the tests eligible in that overlay mode (fs is {} when
                 the file did not certify fs mode for this overlay.py and cpu_count; fs_why)
    children     {id: n} multiprocessing children reported in fs mode (0 if none)
    warm         {id: s} warm CPU of the test inside its file batch ("bcpu", optional)
    seeds        hash seeds the file certified (runs "seed<N>" with hash_seed N)
    startup      the engine's median startup CPU (header startup_cpu), or None
    fs_overhead  p90 CPU of building + removing one overlay (header fs build/remove_cpu)"""

    def __init__(self):
        self.memory, self.fs, self.children, self.warm = {}, {}, {}, {}
        self.fs_why = None
        self.seeds = set()
        self.startup = None
        self.fs_overhead = 0.0


def _read(path):
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"), parse_constant=m0._strict_constant)
    except FileNotFoundError:
        raise Refused(f"no eligibility file {path}") from None
    except Exception as error:
        raise Refused(f"eligibility file does not parse ({type(error).__name__})") from None


def _overlay_digest():
    with open(ovl.__file__, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _fs_problem(header, fs_cpus):
    """None when the header certifies fs mode for this overlay.py and cpu_count."""
    fs = header.get("fs")
    overlays = header.get("overlays")
    if not isinstance(overlays, list) or "fs" not in overlays or not isinstance(fs, dict):
        return "fs mode not certified"
    if fs.get("overlay_digest") != _overlay_digest():
        return "overlay digest differs"
    if fs.get("cpu_count") != fs_cpus:
        return f"fs certified with cpu_count {fs.get('cpu_count')}, not {fs_cpus}"
    if fs.get("mirror") is not None and list(fs.get("mirror")) != list(ovl.MIRROR_DIRS):
        return "overlay mirror differs"
    return None


def _p90(block):
    value = block.get("p90") if isinstance(block, dict) else None
    return float(value) if _number(value) else None


def load(path, tests, src_root, engine, python=None, seeds=(), fs_cpus=None):
    """Eligibility for `engine`; raises Refused (also for a hash seed the file did not
    certify). fs_cpus None: fs mode is not wanted (Eligibility.fs stays empty)."""
    python = tuple(python or sys.version_info[:2])
    data = _read(path)
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
    machine = header.get("machine")
    if machine is not None and hasattr(os, "uname") and machine != os.uname().machine:
        raise Refused(f"certified on {machine}")  # e.g. a Mac file on the linux/amd64 server
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
    out = Eligibility()
    for r in runs:
        s = r.get("hash_seed")
        if isinstance(s, int) and not isinstance(s, bool) and r.get("name") == f"seed{s}":
            out.seeds.add(s)
    for s in seeds:
        if s not in out.seeds:
            raise Refused(f"hash seed {s} not certified (certified: {sorted(out.seeds)})")
    startup = header.get("startup_cpu")
    startup = startup.get(engine) if isinstance(startup, dict) else None
    if isinstance(startup, dict) and _number(startup.get("median")):
        out.startup = float(startup["median"])
    if fs_cpus is not None:
        out.fs_why = _fs_problem(header, fs_cpus)
        if out.fs_why is None:
            fs = header["fs"]
            out.fs_overhead = sum(_p90(fs.get(k)) or 0.0 for k in ("build_cpu", "remove_cpu"))
    for t in tests:
        tid = t["id"]
        e = entries.get(tid)
        got = eligible_entry(e, engine, certified, run_names, "memory")
        if got is not None:
            out.memory[tid] = got
        if fs_cpus is not None and out.fs_why is None:
            got = eligible_entry(e, engine, certified, run_names, "fs")
            if got is not None:
                out.fs[tid] = got
                kids = e["fs"][engine].get("children", 0)
                out.children[tid] = int(kids) if _number(kids) else 1
        warm = e.get("bcpu") if isinstance(e, dict) else None
        warm = warm.get(engine) if isinstance(warm, dict) else None
        if _number(warm):
            out.warm[tid] = float(warm)
    return out


def load_eligibility(path, tests, src_root, engine, overlay="memory", python=None):
    """{test id: (cpu, proc)} of the tests eligible for `engine` in `overlay` mode (fs: with
    the default BR_MODE0_FS_CPUS); raises Refused."""
    if overlay == "fs":
        return load(path, tests, src_root, engine, python=python, fs_cpus=TUNABLES["fs_cpus"][1]).fs
    return load(path, tests, src_root, engine, python=python).memory


# ------------------------------------------------------------------------- CPU estimate

def estimate(items, cfg, warm=False, startup_ref=None):
    """Estimated child CPU of ONE run of a set: the model below times
    cfg.cpu_factor. items: [(cpu, proc, warm_cpu or None, children)] per test, from the
    single-test certification runs of the overlay mode used (proc includes reaped children).

    Legacy (warm False, no children): max_i(proc_i - cpu_i) + sum_i cpu_i: one process
    startup plus every test's cold CPU.
    warm True: max_i(proc_i - cpu_i) + sum_i w_i + max_i(cpu_i - w_i), w_i = the test's CPU
    inside its file batch ("bcpu"): one startup, every test warm, and the largest first-use
    cost (cold minus warm) once; with the warm factor (cfg.cpu_factor). If a test of the set
    has no warm number, the legacy model and factor (cfg.legacy_factor) are used. A set mixes files, so first-use costs of
    several files are paid; the factor covers that (validated, see the module docstring).
    Children (fs mode, tests that start multiprocessing workers): their CPU is in proc_i, not
    in the startup; it is taken as proc_i - cpu_i - startup_ref and summed over such tests."""
    warm = warm and all(w is not None for _, _, w, _ in items)  # else today's estimate
    factor = cfg.cpu_factor if warm else getattr(cfg, "legacy_factor", cfg.cpu_factor)
    startup, own, first_use, kids = 0.0, 0.0, 0.0, 0.0
    for cpu, proc, w, children in items:
        extra = max(proc - cpu, 0.0)
        if children and startup_ref is not None:
            kids += max(extra - startup_ref, 0.0)
            extra = min(extra, startup_ref)
        elif children:
            kids, extra = kids + extra, 0.0
        startup = max(startup, extra)
        if warm:
            w = min(w, cpu)
            own += w
            first_use = max(first_use, cpu - w)
        else:
            own += cpu
    return factor * (startup + own + first_use + kids)


# ----------------------------------------------------------------------------- the gate

class Decision:
    def __init__(self, admit, why, n=0, n_s=0, secs=0.0, delta=0, cpu=0.0, benefit=0.0):
        self.admit, self.why = admit, why
        self.n, self.n_s, self.secs, self.delta, self.cpu, self.benefit = n, n_s, secs, delta, cpu, benefit

    def numbers(self):
        return (f"R={self.n}/{self.n_s} s={self.secs:.3f} d={self.delta}ms "
                f"P*d={self.benefit:.3f} cpu~{self.cpu:.3f} net={self.benefit - self.cpu:+.3f}")


EPS = 1e-9


def gate(selected, R, ms, info, cfg, cpu=None):
    """Expected-value admission for running R (eligible part of `selected`); info: id-free
    list of (cpu, proc) for R. cpu: the CPU of the whole attempt when the caller estimated
    it (every hash-seed run, overlay build/remove); None = the legacy one-run estimate."""
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
    if cpu is None:
        startup = max(max(proc - c, 0.0) for c, proc in info)
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
        self.fs_workdir = None  # None: tempfile.gettempdir() (the image's writable /tmp)
        self.fs_broken = False  # an overlay could not be removed: no more fs runs
        self.site = site_dirs(cfg.site_env)
        if not self.site:
            self._disable("no site-packages directory")

    def _disable(self, reason):
        if self.off is None:
            self.off = reason
            self.log(f"mode0 off: {reason}")

    def eligibility(self):
        if self._elig is None and self.off is None:
            cfg = self.cfg
            try:
                self._elig = load(cfg.elig_path, self.tests, self.src_root, cfg.engine,
                                  python=self.python, seeds=cfg.seeds,
                                  fs_cpus=cfg.fs_cpus if cfg.fs else None)
                self.log(f"mode0: {len(self._elig.memory)} {cfg.engine}-eligible tests")
                if cfg.fs:
                    self.log(f"mode0 fs: {len(self._elig.fs)} eligible tests"
                             + (f" (fs off: {self._elig.fs_why})" if self._elig.fs_why else ""))
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

    def _avail(self):
        """Wall seconds Mode 0 may still use, read from the clock now."""
        cfg = self.cfg
        return min(self.started + self.wall_budget, self.started + self.hard_budget - cfg.reserve) - self.clock()

    def _plan(self, mode, R, selected, ms, elig):
        """(per-run estimate function, Decision) for running R in `mode`."""
        cfg = self.cfg
        table = elig.fs if mode == "fs" else elig.memory
        custom = cfg.warm or mode == "fs" or len(cfg.runs) > 1

        def per_run(nums):
            items = [(*table[self.ids[n]], elig.warm.get(self.ids[n]),
                      elig.children.get(self.ids[n], 0) if mode == "fs" else 0) for n in nums]
            return estimate(items, cfg, warm=cfg.warm, startup_ref=elig.startup)

        total = None
        if custom and R:
            total = per_run(R) * len(cfg.runs) + (elig.fs_overhead if mode == "fs" else 0.0)
        d = gate(selected, R, ms, [table[self.ids[n]] for n in R], cfg, cpu=total)
        d.mode = mode
        return per_run, d

    def _verify(self, cid, selected, res, fps, ms, overlay):
        if self.off is not None or not selected:
            return selected
        if overlay not in OVERLAYS:
            self.log(f"{cid}: mode0 skip (overlay {overlay!r} not supported)")
            return selected
        cfg = self.cfg
        why = contract(fps, res, self.src_root, allow_no_site=cfg.nosite)
        if why is None and cfg.exact:
            why = exact_problem(fps, res, self.src_root)
        if why:
            self.log(f"{cid}: mode0 skip ({why})")
            return selected
        elig = self.eligibility()
        if elig is None:
            return selected
        selected = set(selected)
        order = sorted(selected, key=lambda n: self.order.get(n, n))
        R = [n for n in order if self.ids.get(n) in elig.memory]
        plans = [(R, *self._plan("memory", R, selected, ms, elig))]
        if cfg.fs and elig.fs and not self.fs_broken:
            # A candidate certified only in fs mode (it starts processes): run the whole set in
            # an fs overlay, if that is worth more than the in-memory run of the rest.
            R_fs = [n for n in order if self.ids.get(n) in elig.fs]
            if any(self.ids[n] not in elig.memory for n in R_fs):
                plans.append((R_fs, *self._plan("fs", R_fs, selected, ms, elig)))
        admitted = [p for p in plans if p[2].admit]
        if not admitted:
            for _, _, d in plans:
                tag = "" if d.mode == "memory" else " fs"
                self.log(f"{cid}: mode0{tag} reject ({d.why}) {d.numbers()}")
            return selected
        R, per_run, d = max(admitted, key=lambda p: p[2].benefit - p[2].cpu)
        if self.spent + d.cpu > cfg.cpu_budget + EPS:
            if self.spent >= cfg.cpu_budget:
                self._disable(f"cpu budget spent ({self.spent:.2f}s)")
            self.log(f"{cid}: mode0 reject (cpu budget: spent {self.spent:.2f}s) {d.numbers()}")
            return selected
        avail = self._avail()
        if avail < d.cpu + cfg.margin:
            self.log(f"{cid}: mode0 reject (wall budget: {avail:.1f}s left) {d.numbers()}")
            return selected
        if d.mode == "fs":
            return selected - self._run_fs(cid, R, res, d, per_run)
        spec = {"kind": "mode0", "src": self.src_root,
                "patched": overlay_spec(res.patched, self.src_root),
                "engine": cfg.engine, "site": list(self.site), "startup_cap": cfg.startup_cap}
        return selected - self._runs(cid, R, spec, d, per_run, avail)

    def _run_fs(self, cid, R, res, d, per_run):
        """Build a filesystem overlay of the patched tree, run in it, always remove it."""
        cfg = self.cfg
        workdir = self.fs_workdir or tempfile.gettempdir()
        t0 = time.process_time()
        try:
            root = ovl.build(self.src_root, dict(res.patched), workdir)
        except Exception as error:  # noqa: BLE001 - OverlayError or anything else: keep
            self.spent += time.process_time() - t0
            self.log(f"{cid}: mode0 fs overlay failed {error!r}; kept {d.numbers()}")
            return set()
        try:
            spec = {"kind": "mode0", "engine": cfg.engine, "site": list(self.site),
                    "startup_cap": cfg.startup_cap, **ovl.spec_fields(root)}
            self.spent += time.process_time() - t0
            return self._runs(cid, R, spec, d, per_run, None, cpu_count=cfg.fs_cpus)
        finally:
            t1 = time.process_time()
            try:
                ovl.remove(root)
            except Exception as error:  # noqa: BLE001
                self.log(f"{cid}: mode0 fs overlay {root} not removed {error!r}; fs off")
                self.fs_broken = True
            self.spent += time.process_time() - t1

    def _runs(self, cid, R, spec, d, per_run, avail, cpu_count=None):
        """Run R once per hash seed (cfg.runs), each later run only on the tests that passed
        every earlier one; the tests that passed them all (dropped from the selection)."""
        cfg = self.cfg
        runs = cfg.runs
        alive = list(R)
        tag = "" if d.mode == "memory" else " fs"
        for k, seed in enumerate(runs):
            est = d.cpu if len(runs) == 1 and d.mode == "memory" else per_run(alive)
            if avail is None or k > 0:
                avail = self._avail()  # the overlay build, earlier runs and probes took wall
                if k > 0 and self.spent + est > cfg.cpu_budget + EPS:
                    self.log(f"{cid}: mode0 seed {seed} skipped (cpu budget); kept")
                    return set()
                if avail < est + cfg.margin:
                    self.log(f"{cid}: mode0 seed {seed} skipped (wall budget: {avail:.1f}s left); kept")
                    return set()
            caps = [round(cfg.cap(self.seconds[n]), 3) for n in alive]
            timeout = min(cfg.startup_cap + sum(caps) + cfg.margin, avail)
            run_spec = dict(spec, tests=[{"id": self.ids[n], "cap": cap} for n, cap in zip(alive, caps)])
            kwargs = {}
            if seed is not None:
                kwargs["hash_seed"] = seed
            if cpu_count is not None:
                kwargs["cpu_count"] = cpu_count
            try:
                out = self.execute(run_spec, timeout, **kwargs)
            except Exception as error:  # noqa: BLE001
                self.spent += est  # unknown: charge the estimate
                self.log(f"{cid}: mode0{tag} run failed {error!r}; kept {d.numbers()}")
                return set()
            cpu = out.get("cpu", 0.0) if isinstance(out, dict) else None
            if not _number(cpu):
                cpu = est  # unknown: charge the estimate
            self.spent += cpu
            passed = _passed(out, [self.ids[n] for n in alive])
            survivors = [n for n in alive if self.ids[n] in passed]
            counts = {}
            outcomes = out.get("outcomes") if isinstance(out, dict) else None
            for o in outcomes.values() if isinstance(outcomes, dict) else ():
                counts[str(o)] = counts.get(str(o), 0) + 1
            info = out if isinstance(out, dict) else {}
            wall = info.get("wall") if _number(info.get("wall")) else 0.0
            what = "dropped" if k == len(runs) - 1 else f"seed {seed} passed"
            seed_tag = "" if seed is None else f" seed={seed} est={est:.3f}"
            self.log(f"{cid}: mode0{tag} admit {d.numbers()}{seed_tag} -> {what} {len(survivors)}/{len(alive)} "
                     f"{dict(sorted(counts.items()))} rc={info.get('returncode')} "
                     f"timeout={info.get('timed_out')} cpu={cpu:.3f}s wall={wall:.2f}s spent={self.spent:.2f}s")
            if self.spent >= cfg.cpu_budget:
                self._disable(f"cpu budget spent ({self.spent:.2f}s)")
            alive = survivors
            if not alive:
                return set()
            if k < len(runs) - 1 and self.off is not None:
                self.log(f"{cid}: mode0 later seeds skipped (mode0 off); kept")
                return set()
        return set(alive)


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
