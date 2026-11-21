"""Mode 0: run exactly a commit's candidate tests on the patched code, in one fresh process.

Parent side (stdlib only, cheap to import: no unittest, no pytest):
    execute(spec, timeout)          write the spec, launch the runner, parse its results;
                                    never raises (any problem: every test "notrun")
    launch_argv(spec_path, ...)     the exact command line (one definition for selector and tools)
    parse_results(path, ids, rc)    conservative reading of a result file
    runner_digest(), source_digest(src), tests_identity(tests), env_digest(site_dirs)
                                    identities an eligibility file is bound to

Child side: replica_runner.py dispatches a spec with "kind": "mode0" to run().

Spec (JSON file, the only argument of `python -I -S -B selector/replica_runner.py <spec.json>`,
see launch_argv; -B keeps multiprocessing children from writing .pyc files into src;
launch_argv/execute(cpu_count=N) add `-X cpu_count=N`, which caps ProcessPoolExecutor()):
    {"kind": "mode0",
     "src": "<abs sqlglot root>",
     "patched": {module: [abs path, base64 source]},   # may be empty (eligibility runs)
     "engine": "unittest" | "pytest",
     "site": ["<abs site-packages dir>", ...],         # appended to sys.path after src (no .pth processing)
     "tests": [{"id": "tests/test_x.py::TestX::test_y", "cap": 5.0}, ...],   # run in this order
     "result": "<abs path of result file>",
     "startup_cap": 60.0,                              # optional: wall cap for imports/collection
     "log": "<abs path>"}                              # optional, debugging only: child stdout/stderr

"patched" (memory overlay) is validated by the runner; any violation is a bad spec (exit 2,
nothing passes): every path is absolute, canonical (realpath == path), an existing regular
`.py` file under src/sqlglot/ or src/tests/, not a conftest.py, with identifier components,
each path once, and its key is exactly the module name the path implies (`a/b.py` -> `a.b`,
`a/__init__.py` -> `a`). After the run every patched module present in sys.modules must have
been loaded by the overlay loader, else the run is not complete.

Result file (truncated at start, then one JSON object per line, written with O_APPEND):
    {"start": true, "schema": 1, "engine": ..., "pid": ..., "n": <number of tests>}
    {"id": ..., "outcome": ..., "cpu": s, "wall": s, "flags": [...], "detail": "..."?}   one per test
    {"done": true, "complete": bool, "n": <test lines written>, "counts": {...}, "cpu": s,
     "children_cpu": s, "startup_cpu": s, "wall": s, "engine": ..., "python": "3.13.x",
     "pytest": "9.2.0" | null, "aborted": null | "timeout" | "startup_timeout" |
     "shutdown_timeout" | "shutdown: <why>" | "error: ...", "stopped": null | "<flag>",
     "flags": [...startup flags], "pad": [python depth, C levels], "atexit_new": n,
     "assert_watch": bool}

outcome: passed | failed | error | skipped | timeout | notrun.
  failed  - a failure inside the test case run (setUp, body, subTest, tearDown, cleanups,
            unexpected success): pytest's call phase.
  error   - class/module fixture failure (setUpClass, tearDownClass, class cleanups,
            setUpModule, tearDownModule), collection/import error (also of ANY TestCase class
            of the file: pytest instantiates every one at collection and the organisers then
            fail the whole file), id that does not resolve, or a feature this engine does not
            reproduce.
  skipped - unittest skip / SkipTest anywhere (also inside a subTest) and expected failures
            (pytest reports those as xfailed, i.e. skipped).
  timeout - exceeded its cap; every later test is notrun and the process exits (code 3).
A test's line is written once its outcome is final: after its own teardown and, when it is
the last selected test of its class/module, after that class/module teardown (pytest reports
a tearDownClass failure on the last test of the class). The done line is written only after
the shutdown checks (below) passed.

UNSAFE flags (a pass carrying one is not evidence). Every unsafe flag is STICKY: once one fires
(during a test, a class/module fixture, an import or collection) every later test line of the
process carries it too, because the state it reveals can outlive the test.
  "spawn"         process creation (a child would import unpatched modules from disk), and
                  any attempt to leave the process group/session (os.setsid/setpgrp/setpgid,
                  start_new_session, process_group, posix_spawn setsid/setpgroup: denied).
                  In MEMORY mode every process creation is DENIED (the call raises
                  PermissionError) except multiprocessing's resource tracker (stdlib only).
  "meta_path"     our overlay finder is no longer sys.meta_path[0]  (stops the run)
  "cwd"           cwd changed and not restored                       (stops the run)
  "read_patched"  opened a patched module's file from disk (memory mode), matched by path,
                  realpath, case-insensitive name + (st_dev, st_ino), any os.open(dir_fd=...)
                  of a same-named file, and os.link/os.rename/shutil.copyfile of it. Source
                  readers (inspect.getsource, tracebacks) are served the PATCHED text from
                  linecache (pre-seeded, mtime None) and do not open the file.
  "ctypes"        ctypes asked, from sqlglot/tests code (the first frame outside the ctypes
                  package), to load a library or look up a symbol (dlopen/dlsym: the only
                  way to get a foreign function; calls themselves raise no audit event in
                  3.13) or to touch raw memory (cdata/addressof/string_at) - DENIED; or cffi
                  imported by an import statement in such code (flag only). `import ctypes`
                  itself (numpy, pandas, duckdb do) is fine.
  "subinterp"     sqlglot/tests code imported a subinterpreter module (_interpreters, ...).
                  Those modules are unavailable to everyone (ModuleNotFoundError from a
                  sys.meta_path blocker right after the overlay finder): a subinterpreter
                  imports from disk, and creating one raises no Python audit event.
  "audit_error"   the audit hook itself failed (both modes).
  "assert_rewrite" an AssertionError raised in a module pytest would assertion-rewrite
                  (test_*.py, *_test.py, conftest.py) and the test did not fail: under the
                  organisers' rewriting the error message / evaluation differs (sys.monitoring
                  RAISE watch; without sys.monitoring every line carries it).
  fs mode only: "read_shadowed", "sys_path", "write_pinned", "audit_error" (see below).
Informational: "rtracker" (started multiprocessing's resource tracker, which runs only stdlib
code), "xfail", "subtest_skip", "pytest_import", "unraisable", "mp_spawn", "mp_fork".

unittest engine fidelity (what it reproduces of pytest 9.2's handling of a TestCase): whole-
file collection (module import, then for every TestCase class in the module namespace
cls("runTest") and cls(name) per test method, as pytest does at collection; the instance made
then is the one run), `case(result=...)` with the collected bound method set on the instance,
per-item warnings.catch_warnings(record=True) with pytest's filters around setup (setUpModule/
setUpClass), call and teardown (tearDownClass/tearDownModule) and around collection,
unraisable / thread exceptions turned into warnings after each phase (they fail the phase only
when the filter in force is "error", as in 9.2), sys.stdin that raises on read like pytest's
DontReadFromInput, sys.stdout/sys.stderr replaced (utf-8, errors="replace", not the original
objects), root logger handlers installed (so logging.basicConfig is a no-op, as under pytest)
and, per setup/call/teardown phase, two LogCaptureHandler-like handlers that format every
record and re-raise a formatting error (a bad %-arity log call fails the test, as in pytest),
faulthandler enabled, PYTEST_CURRENT_TEST / PYTEST_VERSION set, the site builtins (exit, quit,
help, ...). Both engines run every phase at a Python frame depth and C recursion depth padded
to at least the organisers' (measured: test body 32 frames, setUpClass 41, module import 38;
~23 C levels) plus a margin (see _PAD_DEPTH / _PAD_C), so recursion limits are hit here first.

Shutdown (both engines): before the done line the runner does what interpreter shutdown would
do in the organisers' pytest process: it runs threading's atexit hooks (e.g. executor
shutdown), waits up to SHUTDOWN_JOIN s for every non-daemon thread, then runs the atexit
callbacks. A non-daemon thread still alive (the organisers' process would hang: lost chunk),
a hang (SHUTDOWN_CAP) or an exit from a callback -> nothing passes. Module finalisers run by a
real interpreter teardown are NOT run (residual: the runner exits with os._exit).

Exit code 0 means the run finished normally (whatever the test outcomes). The consumer must
treat ONLY "passed" as evidence, and only with exit code 0 and a final done line with
"complete": true; see parse_results.

Filesystem overlay mode ("overlay": "fs"; absent or "memory" = the in-memory overlay above,
unchanged). The patched tree exists on disk (selector/overlay.py builds it), so processes the
tests create import the patched files exactly as from a patched checkout:
    {"kind": "mode0", "overlay": "fs",
     "src": "<abs overlay root>",                      # overlay.spec_fields(root) gives these four
     "patched": {},                                    # must be empty
     "shadowed": ["<abs real path of the pinned original of every changed file>", ...],
     "pinned": "<abs pinned root>",                    # optional; enables sys_path/write_pinned
     ...the other fields as above}
Flags in fs mode (memory mode never emits them):
  UNSAFE "read_shadowed"  opened a shadowed original, or the pinned pyc of a changed module
                          (e.g. via os.path.realpath(__file__)), in the runner or a child;
         "sys_path"       a sys.path entry resolves into the pinned tree;
         "write_pinned"   opened a file of the pinned tree for writing (through a symlink);
         "audit_error"    the audit hook itself failed;
         "spawn"          any process creation other than the two below: subprocess, os.system,
                          os.exec*, os.posix_spawn, os.fork outside multiprocessing, the
                          forkserver start method, or a multiprocessing child whose start
                          could not be verified (cwd/sys.path/env/__main__/flags not those of
                          this run). Leaving the process group/session is denied.
  info   "mp_spawn"       a multiprocessing spawn child, verified at creation: argv is exactly
                          [sys.executable, *_args_from_interpreter_flags(), "-c",
                          "from multiprocessing.spawn import spawn_main; spawn_main(...)",
                          "--multiprocessing-fork"], inherited env/cwd/process group, cwd in the
                          overlay, the overlay (and nothing in the pinned tree) on sys.path,
                          __main__ is replica_runner.py. The child re-imports replica_runner.py
                          as __mp_main__, which (env BR_MODE0_FS) checks cwd/sys.path/flags
                          again and installs the same audit hook; anything wrong -> it exits
                          with code 86/87 before running any task (a pool then breaks: the
                          test errors). Its events go to <result>.mp and are folded in here.
         "mp_fork"        os.fork from multiprocessing's popen_fork with the same state
                          checks; the child inherits the hook and reports to <result>.mp.
  Child-side flags (read_shadowed, spawn, ...) are sticky: they are added to the test that is
  running when they are read and to every later test of the run (a pool worker outlives the
  test that started it). At exit the runner kills and reaps its remaining children (so their
  CPU is in the parent's wait4 rusage) and stops multiprocessing's resource tracker gracefully
  (it unlinks leaked semaphores). The done line gains "overlay": "fs", "mp_children" (child
  start reports) and "child_events".
"""

import hashlib
import json
import os
import sys
import time

SCHEMA = 1
OUTCOMES = ("passed", "failed", "error", "skipped", "timeout", "notrun")
UNSAFE_FLAGS = frozenset({"spawn", "meta_path", "cwd", "read_patched",
                          "read_shadowed", "sys_path", "write_pinned", "audit_error",
                          "ctypes", "subinterp", "assert_rewrite"})
STOP_FLAGS = frozenset({"meta_path", "cwd"})
FS_ENV = "BR_MODE0_FS"
HERE = os.path.dirname(os.path.abspath(__file__))
REPLICA_RUNNER = os.path.join(HERE, "replica_runner.py")
EXIT_OK, EXIT_TIMEOUT, EXIT_ABORT, EXIT_BADSPEC = 0, 3, 4, 2

# Interpreter flags. "iso" is what the selector uses (-I ignores PYTHONHASHSEED, so the hash
# seed is random). "seeded" is the closest equivalent that honours PYTHONHASHSEED: -s -P are
# the parts of -I other than -E, and the environment is scrubbed of every other PYTHON* var.
# -B: the runner itself never writes bytecode (sys.dont_write_bytecode), but multiprocessing
# children only inherit the startup flag, and they would write .pyc files into the source tree.
ISO_FLAGS = ("-I", "-S", "-B")
SEEDED_FLAGS = ("-s", "-P", "-S", "-B")

# Frame padding (see the module docstring). Every phase callable (test module import, class
# instantiation, setUpModule, setUpClass, the test case call, class/module teardown, and
# pytest.main) is invoked from Python frame depth _PAD_DEPTH; _PAD_C of the padding levels go
# through a C call, each consuming C recursion budget like pytest's/pluggy's C-level calls.
# Measured on CPython 3.13 / pytest 9.2 (organisers' command line, ground_truth.run_suite):
# test body at depth 32, setUpClass 41, module import 38, ~23 C levels used at the test body.
# With _PAD_DEPTH 50 the unittest engine runs the test body at ~54, setUpClass at ~51, a test
# module body at ~57; the pytest engine ~20-30 deeper than the organisers.
_PAD_DEPTH = 50
_PAD_C = 40

SHUTDOWN_JOIN = 2.0   # seconds to wait for non-daemon threads at shutdown
SHUTDOWN_CAP = 8.0    # wall cap of the whole shutdown phase


# --------------------------------------------------------------------------- parent side

def launch_argv(spec_path, python=None, hash_seed=None, env=None, cpu_count=None):
    """(argv, env) to launch the Mode 0 runner. hash_seed=None is the production launch.

    cpu_count=N adds `-X cpu_count=N` (Python >= 3.13: os.cpu_count() and os.process_cpu_count()
    return N in the runner, so ProcessPoolExecutor() starts at most N workers). 3.13 does not
    pass it on to multiprocessing children (3.14 does); they never size pools themselves.
    """
    python = python or sys.executable
    env = dict(os.environ if env is None else env)
    xopt = [] if cpu_count is None else ["-X", f"cpu_count={int(cpu_count)}"]
    if hash_seed is None:
        return [python, *ISO_FLAGS, *xopt, REPLICA_RUNNER, spec_path], env
    env = {k: v for k, v in env.items() if not k.startswith("PYTHON")}
    env["PYTHONHASHSEED"] = str(int(hash_seed))
    return [python, *SEEDED_FLAGS, *xopt, REPLICA_RUNNER, spec_path], env


def _strict_constant(name):
    raise ValueError(name)


def parse_results(path, expected_ids, returncode=None):
    """{id: outcome} for expected_ids. Only "passed" is evidence that a test passed.

    Conservative: a non-zero returncode, an unreadable/garbled/truncated file, a missing start
    or done line, a done line that is not complete or whose count does not match, any malformed
    line -> no test is "passed". A test with several lines is "duplicate"; a test without a line
    is "missing"; a pass carrying an unsafe flag is "flagged". Ids not expected are ignored.
    Pass the child's exit code as returncode (None only if you checked it yourself).

    A timeout (done "aborted": "timeout") keeps nothing either, not even the passes written
    before the timed-out test: the organisers' process may hang on that test too, and a lost
    pytest chunk fails every test of its files (tools/ground_truth.py).
    """
    expected = list(dict.fromkeys(expected_ids))
    out = {i: "missing" for i in expected}

    def demote():
        return {i: (o if o != "passed" else "unproven") for i, o in out.items()}

    if returncode is not None and returncode != 0:
        return demote()
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8")
    except Exception:
        return demote()
    if not text.endswith("\n"):
        return demote()
    lines = text[:-1].split("\n")
    objs = []
    try:
        for line in lines:
            obj = json.loads(line, parse_constant=_strict_constant)
            if not isinstance(obj, dict):
                return demote()
            objs.append(obj)
    except Exception:
        return demote()
    if len(objs) < 2 or objs[0].get("start") is not True or objs[0].get("schema") != SCHEMA:
        return demote()
    done = objs[-1]
    body = objs[1:-1]
    seen = {}
    valid = True
    for obj in body:
        tid, outcome, flags = obj.get("id"), obj.get("outcome"), obj.get("flags")
        if (not isinstance(tid, str) or outcome not in OUTCOMES or not isinstance(flags, list)
                or not all(isinstance(f, str) for f in flags) or "done" in obj or "start" in obj):
            valid = False
            continue
        seen[tid] = seen.get(tid, 0) + 1
        if tid not in out:
            continue
        if seen[tid] > 1:
            out[tid] = "duplicate"
        elif outcome == "passed" and UNSAFE_FLAGS.intersection(flags):
            out[tid] = "flagged"
        else:
            out[tid] = outcome
    if (not valid or done.get("done") is not True or done.get("complete") is not True
            or done.get("n") != len(body)):
        return demote()
    return out


def _wait_reap(pid):
    """Wait for `pid` to exit without reaping it, kill its process group while the zombie
    leader still holds the pgid (no recycled-pgid race), then reap it: (status, rusage)."""
    import signal

    if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
        while True:
            try:
                os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
                break
            except InterruptedError:
                continue
            except ChildProcessError:
                break
        try:
            os.killpg(pid, signal.SIGKILL)  # leftovers (pool workers, trackers)
        except OSError:
            pass
        while True:
            try:
                _, status, usage = os.wait4(pid, 0)
                return status, usage
            except InterruptedError:
                continue
    while True:  # no waitid: reap first (the pgid may then be reused; best effort)
        try:
            _, status, usage = os.wait4(pid, 0)
            break
        except InterruptedError:
            continue
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    return status, usage


def execute(spec, timeout, python=None, hash_seed=None, workdir=None, env=None, cpu_count=None):
    """Run one Mode 0 spec in a fresh process (own session; the whole group is killed on
    timeout and after exit). spec["result"] is set here. Returns a dict:
        outcomes   {id: outcome} from parse_results (only "passed" is evidence)
        returncode exit code (negative: signal), None if it could not be started
        cpu        user+sys of the child and its reaped descendants (wait4 rusage; an fs-mode
                   runner reaps its multiprocessing children before it exits)
        wall       seconds; timed_out bool; done the done line (dict) or None; lines {id: line}
        error      (only on a launcher problem) short text; every test is then "notrun"
    cpu_count: see launch_argv (None = unchanged launch). Never raises an Exception: an
    unwritable or full workdir, a bad spec or a failed launch give every test "notrun".
    """
    t0 = time.monotonic()
    try:
        ids = [t["id"] for t in spec["tests"]]
    except Exception:
        ids = []
    res = {"outcomes": {i: "notrun" for i in ids}, "returncode": None, "cpu": 0.0, "wall": 0.0,
           "timed_out": False, "done": None, "lines": {}}
    try:
        _execute(spec, timeout, python, hash_seed, workdir, env, cpu_count, ids, res)
    except Exception as error:  # noqa: BLE001 - conservative: nothing is evidence
        res["outcomes"] = {i: "notrun" for i in ids}
        res["error"] = _short(error)
    finally:
        res["wall"] = time.monotonic() - t0
        for p in res.pop("_paths", ()):
            for q in (p, p + ".mp"):
                try:
                    os.remove(q)
                except OSError:
                    pass
    return res


def _execute(spec, timeout, python, hash_seed, workdir, env, cpu_count, ids, res):
    import signal
    import subprocess
    import tempfile
    import threading

    fd, spec_path = tempfile.mkstemp(prefix="br-m0-", suffix=".json", dir=workdir)
    res_path = spec_path[:-5] + ".out"
    res["_paths"] = (spec_path, res_path)
    with os.fdopen(fd, "w") as fh:
        json.dump(dict(spec, result=res_path), fh)
    argv, run_env = launch_argv(spec_path, python=python, hash_seed=hash_seed, env=env,
                                cpu_count=cpu_count)
    try:
        proc = subprocess.Popen(argv, env=run_env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, close_fds=True)
    except Exception as error:
        res["error"] = "launch: " + _short(error)
        return

    def kill():
        res["timed_out"] = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass

    timer = threading.Timer(timeout, kill)
    timer.daemon = True
    timer.start()
    try:
        status, usage = _wait_reap(proc.pid)
    finally:
        timer.cancel()
    proc.returncode = rc = os.waitstatus_to_exitcode(status)
    res["returncode"] = rc
    res["cpu"] = usage.ru_utime + usage.ru_stime
    res["outcomes"] = parse_results(res_path, ids, -9 if res["timed_out"] else rc)
    try:
        with open(res_path, "rb") as fh:
            for line in fh.read().decode("utf-8", "replace").splitlines():
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and obj.get("done") is True:
                    res["done"] = obj
                elif isinstance(obj, dict) and isinstance(obj.get("id"), str):
                    res["lines"].setdefault(obj["id"], obj)
    except OSError:
        pass


def runner_digest():
    """sha256 over the two runner sources; an eligibility file is valid only for these bytes."""
    h = hashlib.sha256()
    for path in (os.path.join(HERE, "mode0_runner.py"), REPLICA_RUNNER):
        with open(path, "rb") as fh:
            data = fh.read()
        h.update(os.path.basename(path).encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


_DIGEST_SKIP_DIRS = frozenset({"__pycache__", ".git"})


def source_digest(src, dirs=None):
    """sha256 binding the CONTENT of every file a test run can read from the pinned tree.

    dirs=None (default): the whole tree under src except .git/ and __pycache__/ (and .pyc/.pyo
    files): sqlglot/, tests/ (fixtures included), sqlglotc/, pyproject.toml, setup.cfg,
    setup.py, docs, ... Over the sorted relative paths: for a regular file its length and bytes;
    for a symlink its target text (and, for a link to a file, the bytes it resolves to); for a
    directory symlink its target (not followed). ~15 ms CPU for the pinned sqlglot tree (warm).
    dirs=("sqlglot", "tests") gives the digest of those directories only (the old scope).
    """
    tops = [src] if dirs is None else [os.path.join(src, d) for d in dirs]
    entries = []
    for top in tops:
        for root, subdirs, names in os.walk(top):
            keep = []
            for s in sorted(subdirs):
                if s in _DIGEST_SKIP_DIRS:
                    continue
                full = os.path.join(root, s)
                if os.path.islink(full):
                    entries.append((os.path.relpath(full, src).replace(os.sep, "/"), "dirlink"))
                else:
                    keep.append(s)
            subdirs[:] = keep
            for name in names:
                if not name.endswith((".pyc", ".pyo")):
                    rel = os.path.relpath(os.path.join(root, name), src).replace(os.sep, "/")
                    entries.append((rel, "file"))
    h = hashlib.sha256()
    for rel, kind in sorted(entries):
        full = os.path.join(src, rel)
        if os.path.islink(full):
            h.update(b"L" + rel.encode() + b"\0" + os.fsencode(os.readlink(full)) + b"\0")
            if kind == "dirlink":
                continue
            try:
                with open(full, "rb") as fh:
                    data = fh.read()
            except OSError:
                h.update(b"dangling\0")
                continue
        else:
            with open(full, "rb") as fh:
                data = fh.read()
        h.update(rel.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def env_digest(site_dirs, version=None, implementation=None, machine=None):
    """sha256 over the interpreter (version, implementation, machine; default: this one) and
    the name-version of every distribution installed in site_dirs (the *.dist-info and
    *.egg-info entry names, per directory in order; paths themselves are not bound). Cheap: one
    listdir per site dir, no importlib.metadata."""
    version = version or sys.version.split()[0]
    implementation = implementation or sys.implementation.name
    machine = machine or (os.uname().machine if hasattr(os, "uname") else "")
    h = hashlib.sha256(f"{version}\0{implementation}\0{machine}\0".encode())
    for k, d in enumerate(site_dirs):
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith((".dist-info", ".egg-info")))
        except OSError:
            names = ["<unreadable>"]
        h.update(f"site{k}\0".encode() + "\0".join(names).encode() + b"\0\0")
    return h.hexdigest()


def tests_identity(tests):
    """sha256 over the ordered (number, id) pairs of a tests.json "tests" list (not durations)."""
    h = hashlib.sha256()
    for t in tests:
        h.update(f"{t['number']}\t{t['id']}\n".encode())
    return h.hexdigest()


def _short(err, limit=300):
    try:
        text = f"{type(err).__name__}: {err}"
    except Exception:
        text = type(err).__name__
    return text[:limit]


# --------------------------------------------------------------------------- child side

_times = os.times
_mono = time.monotonic
_process_time = time.process_time


def _cpu():
    """user+sys CPU of this process (all threads), high resolution."""
    return _process_time()


class _Denied(PermissionError):
    """Raised (from the audit hook or a wrapper) to stop an operation Mode 0 does not allow."""


class _Results:
    """Owns the result file. Every write goes through one lock; once closed (by the main
    thread at the end or by the watchdog on timeout) nothing more is written."""

    def __init__(self, path, ids, engine):
        import threading

        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o644)
        self.lock = threading.Lock()
        self.ids = ids
        self.idset = set(ids)
        self.written = set()
        self.n = 0
        self.counts = {}
        self.closed = False
        self.current = None
        self.engine = engine
        self.t0 = _mono()
        self.startup_cpu = None
        self.startup_flags = set()
        self.sticky = set()        # unsafe flags seen so far: carried by every later line
        self.pytest_version = None
        self.extra = None          # callable -> extra done-line fields
        self.before_done = None    # callable -> None or why the run is not complete
        self.timed_out = False
        self._raw({"start": True, "schema": SCHEMA, "engine": engine, "pid": os.getpid(),
                   "n": len(ids)})

    def _raw(self, obj):
        data = (json.dumps(obj, separators=(",", ":"), allow_nan=False) + "\n").encode()
        while data:
            k = os.write(self.fd, data)
            data = data[k:]

    def _test_line(self, tid, outcome, cpu=0.0, wall=0.0, flags=(), detail=None):
        if tid in self.written or tid not in self.idset:
            return
        flags = set(flags) | self.startup_flags | self.sticky
        self.sticky |= flags & UNSAFE_FLAGS
        obj = {"id": tid, "outcome": outcome, "cpu": round(cpu, 6), "wall": round(wall, 6),
               "flags": sorted(flags)}
        if detail:
            obj["detail"] = str(detail)[:300]
        self._raw(obj)
        self.written.add(tid)
        self.n += 1
        self.counts[outcome] = self.counts.get(outcome, 0) + 1

    def emit(self, tid, outcome, cpu=0.0, wall=0.0, flags=(), detail=None):
        with self.lock:
            if not self.closed:
                self._test_line(tid, outcome, cpu, wall, flags, detail)

    def note_flags(self, flags):
        """Unsafe flags raised outside any test line (between tests): sticky from now on."""
        with self.lock:
            self.sticky |= set(flags) & UNSAFE_FLAGS

    def _done(self, complete, aborted=None, stopped=None):
        t = _times()
        obj = {"done": True, "complete": complete, "n": self.n, "counts": self.counts,
               "cpu": round(t.user + t.system, 6),
               "children_cpu": round(t.children_user + t.children_system, 6),
               "startup_cpu": None if self.startup_cpu is None else round(self.startup_cpu, 6),
               "wall": round(_mono() - self.t0, 6), "engine": self.engine,
               "python": sys.version.split()[0], "pytest": self.pytest_version,
               "aborted": aborted, "stopped": stopped, "flags": sorted(self.startup_flags)}
        if self.extra is not None:
            try:
                obj.update(self.extra())
            except Exception:
                obj["extra_error"] = True
        self._raw(obj)

    def finish(self, complete=True, aborted=None, stopped=None, rest="notrun"):
        """Write `rest` for every test without a line, then (after the shutdown checks) the
        done line, and close."""
        if complete and self.before_done is not None:
            check, self.before_done = self.before_done, None
            try:
                why = check()
            except BaseException as error:  # noqa: BLE001
                why = "check failed: " + _short(error)
            if why:
                complete, aborted = False, "shutdown: " + str(why)[:200]
        with self.lock:
            if self.closed:
                return False
            self.closed = True
            for tid in self.ids:
                self._test_line(tid, rest)
            self._done(complete, aborted, stopped)
            return True

    def abort_timeout(self, phase="test"):
        """Called by the watchdog thread: the current phase timed out."""
        if not self.lock.acquire(timeout=2.0):
            return
        try:
            if self.closed:
                return
            self.closed = True
            self.timed_out = True
            if self.current is not None and phase == "test":
                self._test_line(self.current, "timeout", flags=("timeout",))
            for tid in self.ids:
                self._test_line(tid, "notrun")
            self._done(False, aborted={"test": "timeout", "startup": "startup_timeout"}.get(
                phase, phase + "_timeout"))
        finally:
            self.lock.release()


class _Watchdog:
    """Deadline per test, enforced from a daemon thread (works while the main thread spins in
    Python code), backed by a kernel SIGALRM with the default action (kills the process even if
    C code holds the GIL; the result then has no done line, so nothing counts as passed)."""

    GRACE = 2.0

    def __init__(self, results):
        import signal
        import threading

        self.results = results
        self.signal = signal
        self.cond = threading.Condition()
        self.deadline = None
        self.phase = "test"
        self.before_exit = None  # fs mode: reap children before the timeout exit
        try:
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
        self.thread = threading.Thread(target=self._loop, name="br-mode0-watchdog", daemon=True)
        self.thread.start()

    def arm(self, tid, cap, startup=False, phase=None):
        with self.cond:
            self.results.current = tid
            self.phase = phase or ("startup" if startup else "test")
            self.deadline = _mono() + cap
            self.cond.notify()
        try:
            self.signal.setitimer(self.signal.ITIMER_REAL, cap + max(self.GRACE, 0.5 * cap))
        except (ValueError, OSError):
            pass

    def disarm(self):
        with self.cond:
            self.deadline = None
            self.cond.notify()
        try:
            self.signal.setitimer(self.signal.ITIMER_REAL, 0)
        except (ValueError, OSError):
            pass

    def _loop(self):
        with self.cond:
            while True:
                if self.deadline is None:
                    self.cond.wait()
                    continue
                left = self.deadline - _mono()
                if left > 0:
                    self.cond.wait(left)
                    continue
                phase = self.phase
                break
        self.results.abort_timeout(phase=phase)
        if self.before_exit is not None:
            try:
                self.before_exit()
            except BaseException:
                pass
        os._exit(EXIT_TIMEOUT)


# ------------------------------------------------------------------------ path matching

def _under(path, top):
    return path == top or path.startswith(top + os.sep)


class _Shadow:
    """Matches opens of protected files (memory mode: the on-disk files of patched modules;
    fs mode: the shadowed pinned originals) and of their pinned bytecode.

    A path matches by abspath or realpath; when its basename equals a protected basename
    case-insensitively (or a protected file has other hard links) also by (st_dev, st_ino)."""

    def __init__(self, paths):
        self.files, self.names, self.lower, self.pyc = set(), set(), set(), {}
        self.inodes, self.stat_all = set(), False
        for p in paths:
            for q in {os.path.abspath(p), os.path.realpath(p)}:
                d, name = os.path.split(q)
                self.files.add(q)
                self.names.add(name)
                self.lower.add(name.lower())
                if name.endswith(".py"):
                    self.pyc.setdefault(name[:-3], set()).add(os.path.join(d, "__pycache__"))
            try:
                st = os.stat(p)
                self.inodes.add((st.st_dev, st.st_ino))
                if st.st_nlink > 1:
                    self.stat_all = True
            except OSError:
                pass

    def __bool__(self):
        return bool(self.files)

    def name_hit(self, path):
        """Case-insensitive basename match only (for opens relative to an unknown dir_fd)."""
        return os.path.basename(path).lower() in self.lower

    def hit(self, path):
        name = os.path.basename(path)
        if self.stat_all or name.lower() in self.lower:
            full = os.path.abspath(path)
            if full in self.files or os.path.realpath(full) in self.files:
                return True
            if self.inodes:
                try:
                    st = os.stat(full)
                except (OSError, ValueError):
                    return False
                return (st.st_dev, st.st_ino) in self.inodes
            return False
        if name.endswith(".pyc"):
            dirs = self.pyc.get(name.split(".", 1)[0])
            if dirs:
                full = os.path.abspath(path)
                return (os.path.dirname(full) in dirs
                        or os.path.dirname(os.path.realpath(full)) in dirs)
        return False


# -------------------------------------------------------------------------- the audits

class _Ctx:
    """Per-test observation: flags raised by the audit hook and the post-test checks.

    memory mode (deny=True): every process creation except the resource tracker is denied."""

    read_flag = "read_patched"

    def __init__(self, src, finder, patched_paths, deny=True):
        self.src = src
        self.finder = finder
        self.flags = set()
        self.patched_paths = patched_paths
        self.shadow = _Shadow(patched_paths)
        self.deny = deny
        self.child = False

    def raise_flag(self, flag, **info):
        self.flags.add(flag)

    def check_after(self):
        """Flags for the state a test left behind (meta path / cwd)."""
        out = set()
        try:
            if not sys.meta_path or sys.meta_path[0] is not self.finder:
                out.add("meta_path")
        except Exception:
            out.add("meta_path")
        try:
            if os.getcwd() != self.src:
                out.add("cwd")
        except Exception:
            out.add("cwd")
        return out


_SPAWN_EVENTS = frozenset({
    "subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.posix_spawnp",
    "os.exec", "os.system", "os.spawn", "os.startfile", "pty.spawn", "os.popen",
})
# Foreign calls themselves raise no audit event in 3.13; getting a callable does (dlopen of a
# library, dlsym of a symbol), as do raw memory accessors.
_CTYPES_EVENTS = frozenset({"ctypes.call_function", "ctypes.dlopen", "ctypes.dlsym",
                            "ctypes.dlsym/handle", "ctypes.cdata", "ctypes.cdata/buffer",
                            "ctypes.addressof", "ctypes.string_at", "ctypes.wstring_at"})


def _in_ctypes(filename, src):
    return (isinstance(filename, str) and (os.sep + "ctypes" + os.sep) in filename
            and not filename.startswith(src + os.sep))
_CFFI_MODULES = frozenset({"cffi", "_cffi_backend"})
_COPY_EVENTS = frozenset({"os.link", "os.rename", "shutil.copyfile", "shutil.copytree",
                          "shutil.move"})
_RTRACKER_PREFIX = "from multiprocessing.resource_tracker import main;main("
_SESSION_CALLS = ("setsid", "setpgrp", "setpgid")


def _is_resource_tracker(argv):
    """multiprocessing's resource tracker: `<python> [flags] -c 'from multiprocessing.
    resource_tracker import main;main(<fd>)'`. It imports only the stdlib."""
    argv = [os.fsdecode(a) for a in argv]
    if len(argv) < 3 or argv[-2] != "-c":
        return False
    cmd = argv[-1]
    return (cmd.startswith(_RTRACKER_PREFIX) and cmd.endswith(")")
            and cmd[len(_RTRACKER_PREFIX):-1].isdigit())


def _src_frame(frame, src):
    """True when the Python frame that triggered an event runs sqlglot/tests code under src."""
    if frame is None:
        return False
    fn = frame.f_code.co_filename
    return isinstance(fn, str) and fn.startswith(src + os.sep)


def _common_hook(ctx, event, args):
    """Events handled the same way in both overlay modes. Returns True if handled."""
    if event in _CTYPES_EVENTS:
        # CDLL(...) / lib.symbol: the event comes from ctypes' own frames; judge the first
        # frame outside the ctypes package (import ctypes itself: importlib, not src).
        frame = sys._getframe(2)
        while frame is not None and _in_ctypes(frame.f_code.co_filename, ctx.src):
            frame = frame.f_back
        if _src_frame(frame, ctx.src):
            ctx.raise_flag("ctypes", what=event)
            raise _Denied(f"br-mode0: {event} from sqlglot/tests code is not allowed")
        return True
    if event == "cpython.PyInterpreterState_New":
        ctx.raise_flag("subinterp")
        raise _Denied("br-mode0: subinterpreters are not allowed")
    if event == "import":
        if args and args[0] in _CFFI_MODULES and _src_frame(sys._getframe(2), ctx.src):
            ctx.raise_flag("ctypes", what="cffi")
        return True
    if event in _COPY_EVENTS:
        if args and isinstance(args[0], (str, bytes, os.PathLike)):
            if ctx.shadow.hit(os.fsdecode(args[0])):
                ctx.raise_flag(ctx.read_flag, path=os.fsdecode(args[0])[-200:])
        return True
    return False


_SUBINTERP_MODULES = frozenset({"_interpreters", "interpreters", "concurrent.interpreters",
                                "_interpqueues", "_interpchannels", "_xxsubinterpreters",
                                "_xxinterpchannels", "_xxinterpqueues"})


class _ImportBlocker:
    """sys.meta_path entry right after the overlay finder: subinterpreter modules cannot be
    imported (a subinterpreter imports from disk, unaudited; creating one raises no Python
    audit event): flag "subinterp" and deny."""

    def __init__(self, ctx):
        self.ctx = ctx

    def find_spec(self, fullname, path=None, target=None):
        if fullname in _SUBINTERP_MODULES:
            # "not installed" (stdlib copes: 3.14's concurrent.futures imports _interpreters
            # optionally); flagged when sqlglot/tests code asked for it
            frame = sys._getframe(1)
            while frame is not None and (frame.f_code.co_filename.startswith("<frozen importlib")
                                         or os.sep + "importlib" + os.sep in frame.f_code.co_filename):
                frame = frame.f_back
            if _src_frame(frame, self.ctx.src):
                self.ctx.raise_flag("subinterp", what=fullname)
            raise ModuleNotFoundError(f"br-mode0: {fullname} is not available", name=fullname)
        return None

    def invalidate_caches(self):
        pass


def _block_imports(ctx, index):
    for name in _SUBINTERP_MODULES:
        if name in sys.modules:  # imported before the runner started: make it unusable
            sys.modules[name] = None
    sys.meta_path.insert(index, _ImportBlocker(ctx))


def _wrap_session_calls(ctx):
    """os.setsid/setpgrp/setpgid and posix_spawn(setsid=/setpgroup=) would let a descendant
    leave the runner's process group (killpg would miss it): flag "spawn" and deny."""

    def denied(name):
        def call(*args, **kwargs):
            ctx.raise_flag("spawn", what=name)
            raise _Denied(f"br-mode0: os.{name} is not allowed")
        call.__name__ = name
        return call

    native = sys.modules.get("posix")
    for name in _SESSION_CALLS:
        for mod in (os, native):
            if mod is not None and hasattr(mod, name):
                setattr(mod, name, denied(name))
    for name in ("posix_spawn", "posix_spawnp"):
        original = getattr(os, name, None)
        if original is None:
            continue

        def spawn(*args, _original=original, _name=name, **kwargs):
            if kwargs.get("setsid") or kwargs.get("setpgroup") is not None:
                ctx.raise_flag("spawn", what=_name)
                raise _Denied(f"br-mode0: os.{_name} with setsid/setpgroup is not allowed")
            return _original(*args, **kwargs)
        spawn.__name__ = name
        setattr(os, name, spawn)

    original_open = os.open

    def os_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None and ctx.shadow and isinstance(path, (str, bytes, os.PathLike)):
            if ctx.shadow.name_hit(os.fsdecode(path)):
                ctx.raise_flag(ctx.read_flag, path=os.fsdecode(path)[-200:], dir_fd=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)
    os_open.__name__ = "open"
    os.open = os_open


def _install_spawn_watch(ctx):
    """Memory mode audit: process creation is denied (flag "spawn"), patched files opened
    from disk are flagged (read_patched), plus the common escapes."""
    shadow = ctx.shadow

    def hook(event, args):
        try:
            if event == "open":
                path = args[0]
                if isinstance(path, int) or not shadow:
                    return
                path = os.fsdecode(path)
                if shadow.hit(path):
                    ctx.flags.add("read_patched")
            elif event in _SPAWN_EVENTS:
                ctx.flags.add("spawn")
                raise _Denied(f"br-mode0: process creation ({event}) is denied in memory mode")
            else:
                _common_hook(ctx, event, args)
        except _Denied:
            raise
        except Exception:
            ctx.flags.add("audit_error")  # cannot tell: treat as unsafe

    sys.addaudithook(hook)
    _wrap_session_calls(ctx)

    # multiprocessing's spawn/forkserver start methods and its resource tracker go through
    # _posixsubprocess.fork_exec directly, which raises no audit event.
    import _posixsubprocess

    original = _posixsubprocess.fork_exec

    def fork_exec(*args, **kwargs):
        try:
            tracker = _is_resource_tracker(args[0]) and not args[15] and args[16] == -1
        except Exception:
            tracker = False
        if not tracker:
            ctx.flags.add("spawn")
            raise _Denied("br-mode0: process creation is denied in memory mode")
        ctx.flags.add("rtracker")
        return original(*args, **kwargs)

    _posixsubprocess.fork_exec = fork_exec


# ------------------------------------------------------------ filesystem overlay mode

_MP_SPAWN_PREFIX = "from multiprocessing.spawn import spawn_main; spawn_main("
_STICKY = frozenset({"read_shadowed", "spawn", "write_pinned", "audit_error", "ctypes",
                     "subinterp"})
_CHILD_EVENT_FLAGS = {"read_shadowed": "read_shadowed", "spawn": "spawn", "bad": "spawn",
                      "write_pinned": "write_pinned", "audit_error": "audit_error",
                      "ctypes": "ctypes", "subinterp": "subinterp"}
_WRITE_BITS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


class _FsCtx(_Ctx):
    """fs mode: the Ctx of the runner (child=False) or of a multiprocessing child (True).

    In the runner, flags go to the running test; in a child they are written as events to the
    side file, which the runner reads in check_after()."""

    read_flag = "read_shadowed"

    def __init__(self, root, pinned, shadowed, side, env_value, child=False, side_fd=None):
        super().__init__(root, None, set(), deny=False)
        self.root = root
        self.pinned = os.path.realpath(pinned) if pinned else None
        self.shadow = _Shadow(shadowed)
        self.side = side
        self.env_value = env_value
        self.child = child
        self.side_fd = side_fd
        self.sticky = set()
        self.children = []
        self.mp_started = 0
        self.events = 0
        self._offset = 0
        self._tail = b""

    # -- flags / events
    def raise_flag(self, flag, **info):
        if self.child:
            self.event(dict(info, ev=flag))
            return
        self.flags.add(flag)
        if flag in _STICKY:
            self.sticky.add(flag)

    def event(self, obj):
        obj["pid"] = os.getpid()
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        if len(data) > 512:
            data = (json.dumps({"ev": obj["ev"], "pid": obj["pid"]}) + "\n").encode()
        os.write(self.side_fd, data)

    def drain(self):
        """Fold child events written so far into flags (runner side)."""
        out = set()
        while True:
            try:
                chunk = os.pread(self.side_fd, 1 << 16, self._offset)
            except OSError:
                out.add("audit_error")
                break
            if not chunk:
                break
            self._offset += len(chunk)
            self._tail += chunk
        *lines, self._tail = self._tail.split(b"\n")
        for line in lines:
            self.events += 1
            try:
                ev = json.loads(line).get("ev")
            except Exception:
                out.add("audit_error")
                continue
            if ev == "start":
                self.mp_started += 1
            elif ev in _CHILD_EVENT_FLAGS:
                out.add(_CHILD_EVENT_FLAGS[ev])
            elif ev not in ("rtracker",):
                out.add("audit_error")
        self.sticky |= out & _STICKY
        return out

    # -- state checks
    def pinned_entry(self, entry):
        if not self.pinned:
            return False
        try:
            return _under(os.path.realpath(entry or "."), self.pinned)
        except Exception:
            return True

    def state_problem(self, spawn):
        """Why a multiprocessing child started now would not run against the overlay."""
        try:
            if not _under(os.path.realpath(os.getcwd()), self.root):
                return "cwd outside the overlay"
        except OSError:
            return "no cwd"
        path = list(sys.path)
        if "" in path:
            return "'' on sys.path"
        if self.root not in [os.path.abspath(p) for p in path if isinstance(p, str)]:
            return "overlay not on sys.path"
        if any(not isinstance(p, str) or self.pinned_entry(p) for p in path):
            return "pinned tree on sys.path"
        if os.environ.get(FS_ENV) != self.env_value:
            return "environment changed"
        if spawn:
            main = sys.modules.get("__main__")
            if getattr(main, "__spec__", None) is not None:
                return "__main__ has a spec"
            mfile = getattr(main, "__file__", None)
            if not mfile or os.path.realpath(mfile) != os.path.realpath(REPLICA_RUNNER):
                return "__main__ is not replica_runner.py"
        return None

    def check_after(self):
        out = super().check_after()
        try:
            if any(not isinstance(p, str) or self.pinned_entry(p) for p in list(sys.path)):
                out.add("sys_path")
        except Exception:
            out.add("audit_error")
        out |= self.drain()
        self.sticky |= out & _STICKY
        return out | self.sticky

    # -- children
    def reap(self, grace=2.0):
        """Kill and reap every child still ours, then stop the resource tracker gracefully."""
        import signal

        pids = list(self.children)
        mpp = sys.modules.get("multiprocessing.process")
        for p in list(getattr(mpp, "_children", ()) or ()):
            try:
                pids.append(p.pid)
            except Exception:
                pass
        rt = getattr(sys.modules.get("multiprocessing.resource_tracker"), "_resource_tracker", None)
        tracker = getattr(rt, "_pid", None)
        live = []
        for pid in dict.fromkeys(pids):
            if not isinstance(pid, int) or pid <= 0 or pid == tracker:
                continue
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == 0:  # still our unreaped child
                    os.kill(pid, signal.SIGKILL)
                    live.append(pid)
            except OSError:
                pass
        if tracker:
            try:
                fd = rt._fd
                rt._fd = None
                if fd is not None:
                    os.close(fd)  # EOF: the tracker unlinks what is left, then exits
            except Exception:
                pass
            live.append(tracker)
        deadline = _mono() + grace
        while live:
            for pid in list(live):
                try:
                    done = os.waitpid(pid, os.WNOHANG)[0] != 0
                except OSError:
                    done = True
                if done:
                    live.remove(pid)
            if live and _mono() > deadline:
                for pid in live:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                    except OSError:
                        pass
                break
            if live:
                time.sleep(0.005)
        if rt is not None and tracker:
            rt._pid = None


def _is_mp_spawn(args):
    """fork_exec(...) arguments of util.spawnv_passfds launching a multiprocessing spawn child
    of this interpreter, with inherited env and cwd, in our process group."""
    import subprocess

    if len(args) < 17 or args[4] is not None or args[5] is not None or args[15] or args[16] != -1:
        return False
    argv = [os.fsdecode(a) for a in args[0]]
    exe = [os.fsdecode(a) for a in args[1]]
    if len(argv) < 4 or argv[-1] != "--multiprocessing-fork" or argv[-3] != "-c":
        return False
    prog = argv[-2]
    if not (prog.startswith(_MP_SPAWN_PREFIX) and prog.endswith(")")):
        return False
    inner = prog[len(_MP_SPAWN_PREFIX):-1]
    for item in inner.split(", ") if inner else ():
        key, _, value = item.partition("=")
        if not key.isidentifier() or not (value.isdigit() or value == "None"):
            return False
    return (argv[0] == sys.executable and exe == [sys.executable]
            and argv[1:-3] == subprocess._args_from_interpreter_flags())


def _mp_fork_caller(frame):
    mod = sys.modules.get("multiprocessing.popen_fork")
    return (frame is not None and mod is not None and frame.f_code.co_name == "_launch"
            and frame.f_code.co_filename == getattr(mod, "__file__", None))


def _writes(mode, flags):
    if isinstance(mode, str):
        return any(c in mode for c in "wax+")
    return isinstance(flags, int) and bool(flags & _WRITE_BITS)


def _install_fs_watch(ctx):
    shadow = ctx.shadow
    spawn_events = _SPAWN_EVENTS - {"os.fork"}

    def hook(event, args):
        try:
            if event == "open":
                path = args[0]
                if isinstance(path, int):
                    return
                path = os.fsdecode(path)
                if shadow.hit(path):
                    ctx.raise_flag("read_shadowed", path=path[-200:])
                if (ctx.pinned and len(args) > 2 and _writes(args[1], args[2])
                        and _under(os.path.realpath(os.path.abspath(path)), ctx.pinned)):
                    ctx.raise_flag("write_pinned", path=path[-200:])
            elif event == "os.fork":
                if not ctx.child and _mp_fork_caller(sys._getframe(1)) and ctx.state_problem(False) is None:
                    ctx.raise_flag("mp_fork")
                else:
                    ctx.raise_flag("spawn", what=event)
            elif event in spawn_events:
                ctx.raise_flag("spawn", what=event)
            else:
                _common_hook(ctx, event, args)
        except _Denied:
            raise
        except Exception:
            try:
                ctx.raise_flag("audit_error")
            except Exception:
                pass

    sys.addaudithook(hook)
    _wrap_session_calls(ctx)

    def after_fork_in_child():
        # A forked child (multiprocessing fork, or anything else - flagged in the parent)
        # inherits this hook and reports through the inherited side-file descriptor.
        ctx.child = True
        ctx.children = []

    os.register_at_fork(after_in_child=after_fork_in_child)

    import _posixsubprocess

    original = _posixsubprocess.fork_exec

    def fork_exec(*args, **kwargs):
        try:
            if len(args) > 16 and (args[15] or args[16] != -1):
                ctx.raise_flag("spawn", what="new session/process group")
                raise _Denied("br-mode0: start_new_session/process_group is not allowed")
            if _is_resource_tracker(args[0]):
                kind = "rtracker"
            elif not ctx.child and _is_mp_spawn(args) and ctx.state_problem(True) is None:
                kind = "mp_spawn"
            else:
                kind = "spawn"
        except _Denied:
            raise
        except Exception:
            kind = "spawn"
        if kind == "rtracker" and ctx.child:
            kind = "spawn"  # children reuse the runner's tracker; a new one is unexpected
        ctx.raise_flag(kind, what="fork_exec")
        pid = original(*args, **kwargs)
        if not ctx.child:
            ctx.children.append(pid)
        return pid

    _posixsubprocess.fork_exec = fork_exec


def _fs_config(spec):
    """fs-mode settings from a valid spec, None for the in-memory overlay; raises if invalid."""
    mode = spec.get("overlay", "memory")
    if mode == "memory":
        return None
    if mode != "fs" or spec.get("patched") != {}:
        raise ValueError("overlay")
    shadowed = spec.get("shadowed")
    pinned = spec.get("pinned")
    if (not isinstance(shadowed, list) or not all(isinstance(p, str) and os.path.isabs(p) for p in shadowed)
            or not (pinned is None or (isinstance(pinned, str) and os.path.isabs(pinned)))):
        raise ValueError("shadowed/pinned")
    return {"shadowed": shadowed, "pinned": pinned}


def child_init(env_value):
    """In a multiprocessing spawn (or forkserver) child of an fs-mode runner: called when
    replica_runner.py is imported as __mp_main__, i.e. after multiprocessing set sys.path, argv
    and cwd from the parent and before any task is unpickled. Raises (the caller then exits
    with 86) if the audit cannot be installed; exits 87 if the child is not in the overlay."""
    cfg = json.loads(env_value)
    fd = os.open(cfg["side"], os.O_WRONLY | os.O_APPEND)
    ctx = _FsCtx(cfg["root"], cfg.get("pinned"), cfg["shadowed"], cfg["side"], env_value,
                 child=True, side_fd=fd)
    why = ctx.state_problem(spawn=False)
    f = sys.flags
    if why is None and not (f.dont_write_bytecode and f.no_site and f.no_user_site and f.safe_path):
        why = "interpreter flags"
    if why is not None:
        ctx.event({"ev": "bad", "why": why})
        os._exit(87)
    _install_fs_watch(ctx)
    _block_imports(ctx, 0)
    ctx.event({"ev": "start", "cwd": os.getcwd()[-200:]})


# ------------------------------------------------------------------- spec validation

def _module_for(rel):
    """Module name implied by a src-relative path, or None if it is not importable as such."""
    parts = rel.split("/")
    if (len(parts) < 2 or parts[0] not in ("sqlglot", "tests") or not parts[-1].endswith(".py")
            or parts[-1] == "conftest.py"):
        return None
    parts[-1] = parts[-1][:-3]
    if not all(p.isidentifier() for p in parts):
        return None
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _check_patched(src, patched):
    """Validate the memory overlay map (see the module docstring); raises ValueError."""
    import base64
    import binascii
    import stat

    if not isinstance(patched, dict):
        raise ValueError("patched")
    paths = set()
    for name, entry in patched.items():
        if (not isinstance(name, str) or not isinstance(entry, list) or len(entry) != 2
                or not all(isinstance(x, str) for x in entry)):
            raise ValueError("patched entry")
        path, data = entry
        if not os.path.isabs(path) or os.path.normpath(path) != path or not _under(path, src):
            raise ValueError("patched path outside src")
        rel = os.path.relpath(path, src).replace(os.sep, "/")
        if _module_for(rel) != name:
            raise ValueError(f"patched module name {name!r} does not match {rel!r}")
        try:
            st = os.lstat(path)
        except OSError:
            raise ValueError("patched path missing") from None
        if not stat.S_ISREG(st.st_mode) or os.path.realpath(path) != path:
            raise ValueError("patched path not a regular file / not canonical")
        if path in paths:
            raise ValueError("patched path twice")
        paths.add(path)
        try:
            base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("patched source not base64") from None


def _seed_linecache(patched):
    """linecache entries with the PATCHED text for every patched path (mtime None: kept by
    checkcache), the text the organisers' on-disk file gives (decoded like linecache's
    tokenize.open: coding cookie / BOM, universal newlines)."""
    import base64
    import io
    import linecache
    import tokenize

    for _, (path, data) in patched.items():
        raw = base64.b64decode(data)
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
            text = io.TextIOWrapper(io.BytesIO(raw), encoding, newline=None).read()
        except (SyntaxError, UnicodeDecodeError, LookupError):
            continue  # linecache would fail the same way: leave it to read (and flag)
        lines = text.splitlines(True)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        linecache.cache[path] = (len(raw), None, lines, path)


# ----------------------------------------------------------------- padding / helpers

def _depth():
    """Python frames above the caller (the caller's frame counts as 1)."""
    f, n = sys._getframe(1), 0
    while f is not None:
        n += 1
        f = f.f_back
    return n


try:
    from operator import call as _ccall  # C function: each level costs C recursion budget
except ImportError:  # < 3.11
    def _ccall(fn, *args, **kwargs):
        return fn(*args, **kwargs)


def _pad(n, c, fn, args, kwargs):
    if n <= 0:
        return fn(*args, **kwargs)
    if c > 0:
        return _ccall(_pad, n - 1, c - 1, fn, args, kwargs)
    return _pad(n - 1, 0, fn, args, kwargs)


def _padded(fn, *args, **kwargs):
    """fn(*args, **kwargs) called from Python frame depth >= _PAD_DEPTH, with >= _PAD_C extra
    C-level calls on the stack."""
    n = max(_PAD_DEPTH - _depth(), _PAD_C)
    return _pad(n, _PAD_C, fn, args, kwargs)


def _install_site_builtins():
    """exit, quit, help, copyright, credits, license: what `site` adds (the runner uses -S)."""
    try:
        import builtins
        import site

        if not hasattr(builtins, "exit"):
            site.setquit()
        if not hasattr(builtins, "copyright"):
            site.setcopyright()
        if not hasattr(builtins, "help"):
            site.sethelper()
    except Exception:
        pass


class _AssertWatch:
    """Counts AssertionErrors raised in modules pytest would assertion-rewrite (sys.monitoring
    RAISE events). Under rewriting their message and evaluation differ; a test that does not
    fail although one was raised is therefore not evidence ("assert_rewrite")."""

    TOOL_IDS = (4, 3)

    def __init__(self):
        import fnmatch

        self.hits = 0
        self.ok = False
        self._cache = {}
        self._fn = fnmatch.fnmatchcase
        mon = getattr(sys, "monitoring", None)
        if mon is None:
            return
        for tool in self.TOOL_IDS:
            if mon.get_tool(tool) is None:
                try:
                    mon.use_tool_id(tool, "br-mode0-assert")
                except ValueError:
                    continue
                mon.register_callback(tool, mon.events.RAISE, self._on_raise)
                mon.set_events(tool, mon.events.RAISE)
                self.ok = True
                return

    def _rewritten(self, filename):
        hit = self._cache.get(filename)
        if hit is None:
            name = os.path.basename(filename) if isinstance(filename, str) else ""
            hit = self._cache[filename] = (name == "conftest.py" or self._fn(name, "test_*.py")
                                           or self._fn(name, "*_test.py"))
        return hit

    def _assert_raises(self, code):
        """Offsets of the RAISE_VARARGS that end an `assert` statement in `code`."""
        found = self._cache.get(code)
        if found is None:
            import dis

            found, armed = set(), False
            for ins in dis.get_instructions(code):
                if ins.opname == "LOAD_ASSERTION_ERROR" or (  # 3.14: LOAD_COMMON_CONSTANT
                        ins.opname == "LOAD_COMMON_CONSTANT" and ins.argrepr == "AssertionError"):
                    armed = True
                elif ins.opname == "RAISE_VARARGS" and armed:
                    found.add(ins.offset)
                    armed = False
            self._cache[code] = found
        return found

    def _on_raise(self, code, offset, exc):
        # RAISE also fires in every frame an exception propagates through; count only the
        # frame whose own `assert` statement raised it.
        if (isinstance(exc, AssertionError) and self._rewritten(code.co_filename)
                and offset in self._assert_raises(code)):
            self.hits += 1


def _pytest_warning_filters(warnings):
    # What pytest's warnings plugin does around collection and each test (no ini filters).
    if not sys.warnoptions:
        warnings.filterwarnings("always", category=DeprecationWarning)
        warnings.filterwarnings("always", category=PendingDeprecationWarning)


def _validate(spec):
    if spec.get("engine") not in ("unittest", "pytest"):
        raise ValueError("engine")
    tests = spec.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ValueError("tests")
    ids, caps = [], {}
    for t in tests:
        tid, cap = t.get("id"), t.get("cap")
        if not isinstance(tid, str) or not isinstance(cap, (int, float)) or not 0 < cap < 1e5:
            raise ValueError("test entry")
        ids.append(tid)
        caps[tid] = float(cap)
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate ids")
    if not isinstance(spec.get("src"), str) or not os.path.isdir(spec["src"]):
        raise ValueError("src")
    return ids, caps


def _redirect_stdio(log=None):
    """stdin from /dev/null; stdout/stderr to /dev/null (or, for debugging only, a log file)."""
    devnull = os.open(os.devnull, os.O_RDWR)
    out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644) if log else devnull
    os.dup2(devnull, 0)
    os.dup2(out, 1)
    os.dup2(out, 2)
    for fd in {devnull, out}:
        if fd > 2:
            os.close(fd)


# ---------------------------------------------------------------------------- shutdown

def _shutdown_checks(watchdog, finder, patched, results, atexit_base):
    """What the organisers' interpreter does after pytest returns, as far as it can fail
    their process: threading's atexit hooks, joining non-daemon threads, atexit callbacks.
    Returns None or why the run must not count."""
    import threading

    watchdog.arm(None, SHUTDOWN_CAP, phase="shutdown")
    try:
        problem = None
        if patched:
            for name in patched:
                mod = sys.modules.get(name)
                if mod is None:
                    continue
                loader = getattr(getattr(mod, "__spec__", None), "loader", None)
                if not isinstance(loader, getattr(finder, "loader_class", ())) \
                        or not isinstance(getattr(mod, "__loader__", None), finder.loader_class):
                    problem = f"patched module {name} not loaded by the overlay"
                    break
        # threading._shutdown(): the registered threading atexits (executor shutdown), then
        # join every non-daemon thread (the organisers' process would wait forever).
        for hook in reversed(list(getattr(threading, "_threading_atexits", ()))):
            hook()
        main = threading.main_thread()
        end = _mono() + SHUTDOWN_JOIN
        for t in threading.enumerate():
            if t is main or t.daemon or t is watchdog.thread:
                continue
            t.join(max(0.0, end - _mono()))
        alive = [t.name for t in threading.enumerate()
                 if t is not main and not t.daemon and t.is_alive()]
        if alive and problem is None:
            problem = "non-daemon thread alive: " + ",".join(alive)[:100]
        import atexit

        n = atexit._ncallbacks() if hasattr(atexit, "_ncallbacks") else None
        results.atexit_new = None if n is None else n - atexit_base
        if problem is None and n:
            atexit._run_exitfuncs()  # exceptions are reported to sys.unraisablehook, ignored
        return problem
    finally:
        watchdog.disarm()


# ------------------------------------------------------------------------- entry point

def run(spec, install_overlay):
    """Child entry point (called by replica_runner.py). Returns the exit code."""
    try:
        _redirect_stdio(spec.get("log"))
        ids, caps = _validate(spec)
        fs = _fs_config(spec)
        src = os.path.realpath(spec["src"])
        if fs is None:
            _check_patched(src, spec.get("patched"))
    except Exception:
        return EXIT_BADSPEC
    results = _Results(spec["result"], ids, spec["engine"])
    results.atexit_new = None
    ctx = None
    try:
        import atexit

        atexit_base = atexit._ncallbacks() if hasattr(atexit, "_ncallbacks") else 0
        os.environ["SKIP_INTEGRATION"] = "0"
        for var in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DEBUG", "PYTEST_CURRENT_TEST",
                    "PYTEST_VERSION"):
            os.environ.pop(var, None)
        os.environ.pop(FS_ENV, None)
        if fs is None:
            patched_paths = set()
            for path, _ in spec["patched"].values():
                patched_paths.add(os.path.abspath(path))
                patched_paths.add(os.path.realpath(path))
            ctx = _Ctx(src, None, patched_paths)
            _install_spawn_watch(ctx)
        else:
            side = spec["result"] + ".mp"
            fd = os.open(side, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND, 0o600)
            env_value = json.dumps({"side": side, "root": src, "pinned": fs["pinned"],
                                    "shadowed": fs["shadowed"]}, separators=(",", ":"))
            ctx = _FsCtx(src, fs["pinned"], fs["shadowed"], side, env_value, side_fd=fd)
            os.environ[FS_ENV] = env_value
            _install_fs_watch(ctx)
        watch = _AssertWatch()
        if not watch.ok:
            results.startup_flags.add("assert_rewrite")  # cannot tell: nothing is evidence
        watchdog = _Watchdog(results)
        extra = {"pad": [_PAD_DEPTH, _PAD_C], "assert_watch": watch.ok}
        if fs is not None:
            watchdog.before_exit = ctx.reap
            results.extra = lambda: dict(extra, overlay="fs", mp_children=ctx.mp_started,
                                         child_events=ctx.events, atexit_new=results.atexit_new)
        else:
            results.extra = lambda: dict(extra, atexit_new=results.atexit_new)
        watchdog.arm(None, float(spec.get("startup_cap", 60.0)), startup=True)
        ctx.finder = install_overlay(src, spec["patched"])
        _block_imports(ctx, 1)
        if fs is None and spec["patched"]:
            _seed_linecache(spec["patched"])
        for d in spec.get("site", []):
            sys.path.append(d)
        _install_site_builtins()
        results.before_done = lambda: _shutdown_checks(watchdog, ctx.finder, spec["patched"],
                                                       results, atexit_base)
        if spec["engine"] == "unittest":
            code = _run_unittest(ids, caps, results, watchdog, ctx, watch, spec.get("site", []))
        else:
            code = _run_pytest(ids, caps, results, watchdog, ctx, src, watch)
        if fs is not None and results.timed_out:
            # The watchdog closed the results and is reaping children; killed pool workers
            # can make the timed-out test return here first: keep the timeout exit code.
            code = EXIT_TIMEOUT
        return code
    except BaseException as error:  # noqa: BLE001 - anything unexpected: nothing passes
        results.finish(complete=False, aborted="error: " + _short(error))
        return EXIT_ABORT
    finally:
        if fs is not None and isinstance(ctx, _FsCtx) and not ctx.child:
            ctx.reap()


def _assert_flag(watch, hits0, outcome, flags):
    if watch.hits != hits0 and outcome not in ("failed", "error"):
        flags.add("assert_rewrite")


# ----------------------------------------------------------------- unittest engine

_PYTEST_ONLY_CLASS = ("setup_method", "teardown_method", "setup_class", "teardown_class",
                      "pytestmark")
_PYTEST_ONLY_MODULE = ("setup_module", "teardown_module", "pytestmark", "pytest_plugins")


class _Entry:
    __slots__ = ("id", "module", "cls", "method", "error", "instance", "bound", "file")

    def __init__(self, tid):
        self.id, self.module, self.cls, self.method, self.error = tid, None, None, None, None
        self.instance = self.bound = self.file = None


class _NoStdin:
    """sys.stdin as pytest's capture installs it (_pytest.capture.DontReadFromInput)."""

    _MSG = "pytest: reading from stdin while output is captured!  Consider using `-s`."

    @property
    def encoding(self):
        return sys.__stdin__.encoding

    def read(self, size=-1):
        raise OSError(self._MSG)

    readline = read

    def __next__(self):
        return self.readline()

    def readlines(self, hint=-1):
        raise OSError(self._MSG)

    def __iter__(self):
        return self

    def fileno(self):
        import io
        raise io.UnsupportedOperation("redirected stdin is pseudofile, has no fileno()")

    def flush(self):
        import io
        raise io.UnsupportedOperation("redirected stdin is pseudofile, has no flush()")

    def isatty(self):
        return False

    def close(self):
        pass

    def readable(self):
        return False

    def seek(self, offset, whence=0):
        import io
        raise io.UnsupportedOperation("redirected stdin is pseudofile, has no seek(int)")

    def seekable(self):
        return False

    def tell(self):
        import io
        raise io.UnsupportedOperation("redirected stdin is pseudofile, has no tell()")

    def truncate(self, size=None):
        import io
        raise io.UnsupportedOperation("cannot truncate stdin")

    def write(self, data):
        import io
        raise io.UnsupportedOperation("cannot write to stdin")

    def writelines(self, lines):
        import io
        raise io.UnsupportedOperation("Cannot write to stdin")

    def writable(self):
        return False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    @property
    def buffer(self):
        return self

    closed = None


def _pytest_like_process(site):
    """The process-wide state pytest 9.2 sets up for a session that a TestCase can observe."""
    import faulthandler
    import io
    import logging
    import threading

    class EncodedFile(io.TextIOWrapper):
        __slots__ = ()

        @property
        def name(self):
            return repr(self.buffer)

        @property
        def mode(self):
            return self.buffer.mode.replace("b", "")

    def capture(fd):
        raw = io.FileIO(os.dup(fd), "r+" if fd != 2 else "r+")
        return EncodedFile(raw, encoding="utf-8", errors="replace", newline="", write_through=True)

    sys.stdin = _NoStdin()
    sys.stdout = capture(1)
    sys.stderr = capture(2)
    try:
        faulthandler.enable(file=os.dup(2))
    except Exception:
        pass
    class QuietFileHandler(logging.FileHandler):  # _pytest.logging._FileHandler
        def handleError(self, record):
            pass

    class QuietNullHandler(logging.NullHandler):  # _LiveLoggingNullHandler
        def handleError(self, record):
            pass

    devnull = QuietFileHandler(os.devnull, mode="w", encoding="UTF-8")
    devnull.setFormatter(logging.Formatter(_LOG_FORMAT))
    for handler in (QuietNullHandler(), devnull):
        for logger in _log_targets(logging):
            logger.addHandler(handler)
    for d in site:
        try:
            for n in os.listdir(d):
                if n.startswith("pytest-") and n.endswith(".dist-info"):
                    os.environ["PYTEST_VERSION"] = n[len("pytest-"):-len(".dist-info")]
                    break
        except OSError:
            pass
        if "PYTEST_VERSION" in os.environ:
            break

    pending = []

    def unraisable(u):
        try:
            err = "Exception ignored in" if u.err_msg is None else u.err_msg
            pending.append(f"{err}: {u.object!r}\n\n{u.exc_type.__name__}: {u.exc_value}")
        except BaseException as error:  # noqa: BLE001
            pending.append(f"Exception ignored ({type(error).__name__})")

    def thread_exc(args):
        name = "<unknown>" if args.thread is None else args.thread.name
        pending.append(f"Exception in thread {name}\n\n{args.exc_type.__name__}: {args.exc_value}")

    sys.unraisablehook = unraisable
    threading.excepthook = thread_exc
    return pending


_LOG_FORMAT = "%(levelname)-8s %(name)s:%(filename)s:%(lineno)d %(message)s"


def _log_targets(logging):
    """The root logger and every non-propagating logger (pytest's catching_logs)."""
    root = logging.getLogger()
    out = [root]
    for logger in list(root.manager.loggerDict.values()):
        if isinstance(logger, logging.Logger) and not logger.propagate and logger is not root:
            out.append(logger)
    return out


class _LogPhase:
    """pytest's per-phase log capture (setup, call, teardown): two LogCaptureHandlers (caplog
    and report) at level NOTSET on the root and every non-propagating logger. They format every
    record that reaches them and RE-RAISE a formatting error (logging.raiseExceptions), which
    fails the phase, where plain logging would print "--- Logging error ---" and go on."""

    def __init__(self):
        import io
        import logging

        self.logging = logging

        class LogCaptureHandler(logging.StreamHandler):
            def __init__(self):
                super().__init__(io.StringIO())

            def reset(self):
                self.stream = io.StringIO()

            def handleError(self, record):
                if logging.raiseExceptions:
                    raise  # noqa: PLE0704 - exactly what pytest does

        self.handlers = [LogCaptureHandler(), LogCaptureHandler()]
        for h in self.handlers:
            h.setFormatter(logging.Formatter(_LOG_FORMAT))
        self.attached = []

    def __enter__(self):
        for h in self.handlers:
            h.reset()
            for logger in _log_targets(self.logging):
                logger.addHandler(h)
                self.attached.append((logger, h))
        return self

    def __exit__(self, *exc):
        for logger, h in self.attached:
            logger.removeHandler(h)
        self.attached.clear()
        return False


class _UnraisableWarning(UserWarning):
    """Stands in for pytest.PytestUnraisableExceptionWarning / ...ThreadExceptionWarning."""


def _collect_file(path, src, loader):
    """Import a test file and collect it like pytest: (module, {(attr, method): (instance,
    bound)}) or an error text. Every TestCase class bound in the module namespace (imported
    ones too) is instantiated with "runTest" and once per test method, as pytest does."""
    import importlib
    import inspect
    import unittest
    import warnings

    modname = path[:-3].replace("/", ".")
    try:
        with warnings.catch_warnings(record=True):
            _pytest_warning_filters(warnings)
            mod = _padded(importlib.import_module, modname)
    except BaseException as error:  # noqa: BLE001 - pytest: collection error
        if not isinstance(error, Exception):
            raise
        return None, None, "collect: " + _short(error)
    if os.path.realpath(getattr(mod, "__file__", "") or "") != os.path.realpath(os.path.join(src, path)):
        return mod, None, "module file mismatch"
    items = {}

    def collect_classes():  # one padded call: the instantiations run at depth >= _PAD_DEPTH
        for name, obj in list(vars(mod).items()):
            try:
                if not issubclass(obj, unittest.TestCase):
                    continue
            except Exception:
                continue
            if inspect.isabstract(obj) or not getattr(obj, "__test__", True):
                continue
            obj("runTest")
            found = False
            for mname in loader.getTestCaseNames(obj):
                if not getattr(getattr(obj, mname), "__test__", True):
                    continue
                inst = obj(mname)
                bound = getattr(inst, mname)
                getattr(bound, "pytestmark", None)
                getattr(bound, "__dict__", None)
                items[(name, mname)] = (inst, bound)
                found = True
            if not found and getattr(obj, "runTest", None) is not None:
                inst = obj("runTest")
                items[(name, "runTest")] = (inst, getattr(inst, "runTest"))

    try:
        with warnings.catch_warnings(record=True):
            _pytest_warning_filters(warnings)
            _padded(collect_classes)
    except BaseException as error:  # noqa: BLE001
        if not isinstance(error, Exception):
            raise
        return mod, None, "collect class: " + _short(error)
    return mod, items, None


def _collect_unittest(ids, src):
    """Resolve ids like pytest would collect them; unresolvable or unsupported -> error.

    Whole files are collected (an error anywhere in a file is an error for all its ids)."""
    import unittest

    loader = unittest.TestLoader()
    files = {}
    entries = []
    if os.path.exists(os.path.join(src, "__init__.py")):
        bad_root = "src root is a package (pytest would import differently)"
    else:
        bad_root = None
    for tid in ids:
        e = _Entry(tid)
        entries.append(e)
        parts = tid.split("::")
        if len(parts) != 3 or not all(parts) or bad_root:
            e.error = bad_root or "not a File::Class::method id"
            continue
        path, cname, mname = parts
        if (not path.endswith(".py") or path.startswith("/") or "\\" in path or ".." in path.split("/")
                or "[" in tid or not cname.isidentifier() or not mname.isidentifier()):
            e.error = "unsupported id"
            continue
        dirs = path.split("/")[:-1]
        if not dirs or not all(os.path.isfile(os.path.join(src, *dirs[:k + 1], "__init__.py"))
                               for k in range(len(dirs))):
            e.error = "test file not inside a package chain rooted at src"
            continue
        if not os.path.isfile(os.path.join(src, path)):
            e.error = "test file missing"
            continue
        if path not in files:
            files[path] = _collect_file(path, src, loader)
        mod, items, error = files[path]
        e.file = path
        if error:
            e.error = error
            continue
        try:
            if any(hasattr(mod, name) for name in _PYTEST_ONLY_MODULE):
                e.error = "module uses pytest-only features"
                continue
            cls = getattr(mod, cname, None)
            if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)):
                e.error = "not a unittest.TestCase class"
                continue
            if (cname, mname) not in items:
                e.error = "method not collected"
                continue
            if any(hasattr(cls, name) for name in _PYTEST_ONLY_CLASS):
                e.error = "class uses pytest-only features"
                continue
            func = getattr(cls, mname)
            if hasattr(func, "pytestmark"):
                e.error = "method not collected / pytest marks"
                continue
        except Exception as error:
            e.error = "collect: " + _short(error)
            continue
        e.module, e.cls, e.method = mod, cls, mname
        e.instance, e.bound = items[(cname, mname)]
    # a collection error anywhere in a file fails the whole file for the organisers
    bad = {e.file for e in entries if e.error and e.file and files.get(e.file, (0, 0, None))[2]}
    for e in entries:
        if e.error is None and e.file in bad:
            e.error = "collection error in file"
    return entries


def _call_optional(fn, arg):
    """pytest's _call_with_optional_argument: fn(arg) if fn takes one positional argument."""
    import inspect

    try:
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        params = []
    return _padded(fn, arg) if len(params) == 1 else _padded(fn)


def _run_unittest(ids, caps, results, watchdog, ctx, watch, site):
    import unittest
    import warnings
    from unittest.case import _SubTest

    pending = _pytest_like_process(site)
    log_phase = _LogPhase()
    entries = _collect_unittest(ids, ctx.src)
    if "pytest" in sys.modules or "_pytest" in sys.modules:
        # A test module imported pytest: this engine does not reproduce pytest features.
        for e in entries:
            e.error = e.error or "pytest imported during collection"
    results.startup_flags |= ctx.flags
    ctx.flags = set()
    stop = ctx.check_after()
    results.startup_flags |= stop  # memory mode: check_after only returns STOP_FLAGS
    stop &= STOP_FLAGS
    if stop:
        results.finish(stopped=sorted(stop)[0], rest="error")
        return EXIT_OK
    watchdog.disarm()
    results.startup_cpu = _cpu()

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.ok = 0
            self.sub_skips = 0

        def addSuccess(self, test):
            self.ok += 1

        def addSkip(self, test, reason):
            if isinstance(test, _SubTest):
                self.sub_skips += 1
            super().addSkip(test, reason)

    def phase_warnings():
        """pytest's collect_unraisable/collect_thread_exception after a phase."""
        while pending:
            warnings.warn(_UnraisableWarning(pending.pop(0)))

    valid = [e for e in entries if e.error is None]
    nxt = {id(a): b for a, b in zip(valid, valid[1:])}
    state = {"module": None, "module_status": None, "cls": None, "cls_status": None}

    def module_setup(mod):
        state["module"], state["module_status"] = mod, ("ok", None)
        fn = getattr(mod, "setUpModule", None)
        if fn is None:
            return
        try:
            _call_optional(fn, mod)
        except KeyboardInterrupt:
            raise
        except unittest.SkipTest as error:
            state["module_status"] = ("skipped", "setUpModule skip: " + _short(error))
        except BaseException as error:  # noqa: BLE001 - pytest: setup error
            state["module_status"] = ("error", "setUpModule: " + _short(error))

    def module_teardown():
        mod, status = state["module"], state["module_status"]
        state["module"] = state["module_status"] = None
        if mod is None or status[0] != "ok":
            return None
        fn = getattr(mod, "tearDownModule", None)
        if fn is None:
            return None
        try:
            _call_optional(fn, mod)
        except KeyboardInterrupt:
            raise
        except BaseException as error:  # noqa: BLE001
            return "tearDownModule: " + _short(error)
        return None

    def class_setup(cls):
        state["cls"], state["cls_status"] = cls, ("ok", None)
        if getattr(cls, "__unittest_skip__", False):
            state["cls_status"] = ("skipped", "class skipped: " + str(getattr(cls, "__unittest_skip_why__", "")))
            return
        try:
            _padded(cls.setUpClass)
        except KeyboardInterrupt:
            raise
        except BaseException as error:  # noqa: BLE001
            if isinstance(error, Exception):  # unittest/pytest clean up only for Exception
                try:
                    cls.doClassCleanups()
                except Exception:
                    pass
            if isinstance(error, unittest.SkipTest):
                state["cls_status"] = ("skipped", "setUpClass skip: " + _short(error))
            else:
                state["cls_status"] = ("error", "setUpClass: " + _short(error))

    def class_teardown():
        cls, status = state["cls"], state["cls_status"]
        state["cls"] = state["cls_status"] = None
        if cls is None or status[0] != "ok":
            return None
        problem = None
        try:
            _padded(cls.tearDownClass)
        except KeyboardInterrupt:
            raise
        except BaseException as error:  # noqa: BLE001
            problem = "tearDownClass: " + _short(error)
        finally:
            try:
                cls.doClassCleanups()
            except Exception as error:
                problem = problem or "doClassCleanups: " + _short(error)
            if getattr(cls, "tearDown_exceptions", None):
                problem = problem or "class cleanup: " + _short(cls.tearDown_exceptions[0][1])
        return problem

    def call_case(e, result):
        case, name = e.instance, e.method
        setattr(case, name, e.bound)
        try:
            case(result=result)
        finally:
            delattr(case, name)

    for e in entries:
        if e.error is not None:
            results.emit(e.id, "error", detail=e.error)
            continue
        ctx.flags = flags = set()
        watchdog.arm(e.id, caps[e.id])
        cpu0, wall0 = _cpu(), _mono()
        hits0 = watch.hits
        outcome, detail = None, None
        env = os.environ
        try:
            with warnings.catch_warnings(record=True):
                _pytest_warning_filters(warnings)
                # ---- setup phase: module/class fixtures
                env["PYTEST_CURRENT_TEST"] = f"{e.id} (setup)"
                with log_phase:
                    if state["module"] is not e.module:
                        module_setup(e.module)
                    if state["module_status"][0] == "ok" and state["cls"] is not e.cls:
                        class_setup(e.cls)
                try:
                    phase_warnings()
                except Exception as error:
                    outcome, detail = "error", "setup: " + _short(error)
                mstat, cstat = state["module_status"], state["cls_status"]
                if outcome is not None:
                    pass
                elif mstat[0] != "ok":
                    outcome, detail = mstat
                elif cstat is not None and cstat[0] != "ok":
                    outcome, detail = cstat
                else:
                    # ---- call phase: TestCase.__call__ (setUp, body, tearDown, cleanups)
                    env["PYTEST_CURRENT_TEST"] = f"{e.id} (call)"
                    result = Result()
                    try:
                        with log_phase:
                            _padded(call_case, e, result)
                        phase_warnings()
                    except KeyboardInterrupt:
                        raise
                    except BaseException as error:  # noqa: BLE001 - pytest: call failed
                        result.errors.append((None, "call: " + _short(error)))
                    if result.errors or result.failures or result.unexpectedSuccesses:
                        bad = (result.errors + result.failures)[:1]
                        outcome = "failed"
                        detail = (bad[0][1].strip().splitlines()[-1] if bad else "unexpected success")
                    elif result.skipped:
                        outcome, detail = "skipped", str(result.skipped[0][1])
                        if result.sub_skips:
                            flags.add("subtest_skip")
                    elif result.expectedFailures:
                        outcome, detail = "skipped", "expected failure"
                        flags.add("xfail")
                    elif result.ok == 1 and result.testsRun == 1:
                        outcome = "passed"
                    else:
                        outcome, detail = "error", "no outcome recorded"
                    if getattr(unittest.case, "_module_cleanups", None):
                        outcome, detail = "error", "addModuleCleanup is not reproduced"
                # ---- teardown phase: the instance goes, then class/module fixtures
                env["PYTEST_CURRENT_TEST"] = f"{e.id} (teardown)"
                e.instance = e.bound = None
                following = nxt.get(id(e))
                with log_phase:
                    if following is None or following.cls is not e.cls or following.module is not e.module:
                        problem = class_teardown()
                        if problem and outcome in ("passed", "skipped"):
                            outcome, detail = "error", problem
                    if following is None or following.module is not e.module:
                        problem = module_teardown()
                        if problem and outcome in ("passed", "skipped"):
                            outcome, detail = "error", problem
                try:
                    phase_warnings()
                except Exception as error:
                    if outcome in ("passed", "skipped"):
                        outcome, detail = "error", "teardown: " + _short(error)
                env.pop("PYTEST_CURRENT_TEST", None)
        except BaseException as error:  # noqa: BLE001 - KeyboardInterrupt etc.: abort the run
            watchdog.disarm()
            results.emit(e.id, "error", _cpu() - cpu0, _mono() - wall0, flags, "aborted: " + _short(error))
            results.finish(complete=False, aborted="error: " + _short(error))
            return EXIT_ABORT
        watchdog.disarm()
        if "pytest" in sys.modules or "_pytest" in sys.modules:
            flags.add("pytest_import")
            if outcome == "passed":
                outcome, detail = "error", "test imported pytest"
        flags |= ctx.check_after()
        _assert_flag(watch, hits0, outcome, flags)
        results.emit(e.id, outcome, _cpu() - cpu0, _mono() - wall0, flags, detail)
        stop = flags & STOP_FLAGS
        if stop:
            results.finish(stopped=sorted(stop)[0])
            return EXIT_OK
    results.finish()
    return EXIT_OK


# ------------------------------------------------------------------- pytest engine

def _collect_error_files(nodeids, files):
    """Test files a collection error covers (tools/ground_truth.py _collect_error_hits)."""
    out = set()
    for nodeid in nodeids:
        path = nodeid.split("::")[0].rstrip("/")
        if path in ("", "."):
            return set(files)
        out |= {f for f in files if f == path or f.startswith(path + "/")}
    return out


def _run_pytest(ids, caps, results, watchdog, ctx, src, watch):
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    import pytest

    results.pytest_version = pytest.__version__
    wanted = set(ids)
    reports = {}
    collect_errors = []
    state = {"started": False, "stop": None}

    def startup_done(session):
        if state["started"]:
            return
        state["started"] = True
        results.startup_flags |= ctx.flags
        results.startup_cpu = _cpu()
        stop = ctx.check_after()
        results.startup_flags |= stop
        stop &= STOP_FLAGS
        if stop:  # collection displaced the overlay: nothing here is evidence
            state["stop"] = sorted(stop)[0]
            session.shouldstop = "br-mode0: " + state["stop"]

    class Plugin:
        @pytest.hookimpl(tryfirst=True)
        def pytest_collectreport(self, report):
            if report.failed:
                collect_errors.append(report.nodeid)

        @pytest.hookimpl(trylast=True)
        def pytest_collection_modifyitems(self, session, config, items):
            # Whole files are collected; keep exactly the requested items, once each, in the
            # requested order (pytest's class/module fixture scoping follows this sequence).
            # Items of a file with a collection error are never run: the organisers fail the
            # whole file, so they are "error" before any test runs.
            startup_done(session)
            bad = _collect_error_files(collect_errors, files)
            for tid in ids:
                if tid.split("::")[0] in bad:
                    results.emit(tid, "error", detail="collection error in file")
            keep, seen = [], set()
            for item in items:
                if item.nodeid in wanted and item.nodeid not in seen \
                        and item.nodeid.split("::")[0] not in bad:
                    seen.add(item.nodeid)
                    keep.append(item)
            keep.sort(key=lambda item: order[item.nodeid])
            items[:] = keep

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            tid = item.nodeid
            startup_done(item.session)
            ctx.flags = flags = set()
            reports[tid] = []
            watchdog.arm(tid, caps[tid])
            cpu0, wall0 = _cpu(), _mono()
            hits0 = watch.hits
            try:
                return (yield)
            finally:
                watchdog.disarm()
                cpu, wall = _cpu() - cpu0, _mono() - wall0
                flags |= ctx.check_after()
                outcome, detail = _pytest_outcome(reports.get(tid, []), flags)
                _assert_flag(watch, hits0, outcome, flags)
                results.emit(tid, outcome, cpu, wall, flags, detail)
                stop = flags & STOP_FLAGS
                if stop:
                    state["stop"] = sorted(stop)[0]
                    item.session.shouldstop = "br-mode0: " + state["stop"]

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_logreport(self, report):
            lst = reports.get(report.nodeid)
            if lst is not None:
                lst.append((report.when, report.outcome, type(report).__name__,
                            hasattr(report, "wasxfail"),
                            report.longreprtext[-300:] if report.failed else ""))

    # Pass test FILES, not node ids: pytest aborts the whole session (usage error) on a node
    # id it cannot find or whose module fails to import. With files, a collection error is a
    # collect report (--continue-on-collection-errors) and an unknown id just gets no item.
    order = {tid: k for k, tid in enumerate(ids)}
    files = []
    for tid in ids:
        path = tid.split("::")[0]
        if "::" in tid and path.endswith(".py") and os.path.isfile(os.path.join(src, path)):
            if path not in files:
                files.append(path)
        else:
            results.emit(tid, "error", detail="test file missing")
    if not files:
        results.finish()
        return EXIT_OK
    args = ["-p", "no:cacheprovider", "-o", "addopts=", "-q", "--assert=plain",
            "--continue-on-collection-errors", f"--rootdir={src}", *files]
    try:
        ret = _padded(pytest.main, args, plugins=[Plugin()])
    except BaseException as error:  # noqa: BLE001
        results.finish(complete=False, aborted="error: pytest " + _short(error))
        return EXIT_ABORT
    watchdog.disarm()
    code = int(ret)
    if results.startup_cpu is None:
        results.startup_cpu = _cpu()
    bad = _collect_error_files(collect_errors, files)
    for tid in ids:
        if tid.split("::")[0] in bad:
            results.emit(tid, "error", detail="collection error")
    if state["stop"]:
        results.finish(stopped=state["stop"])
        return EXIT_OK
    if code not in (0, 1, 4, 5):  # interrupted, internal error, ...
        results.finish(complete=False, aborted=f"error: pytest exit {code}")
        return EXIT_ABORT
    results.finish(rest="notrun")
    return EXIT_OK


def _pytest_outcome(reps, flags):
    """Outcome of one item from all its reports (setup, call, teardown, subtests)."""
    if not reps:
        return "notrun", "no reports"
    for when, outcome, kind, xfail, text in reps:
        if outcome == "failed":
            last = text.strip().splitlines()[-1] if text.strip() else ""
            if when == "call":
                return "failed", f"{kind} call: {last}"
            return "error", f"{when}: {last}"
    if any(r[3] for r in reps):
        flags.add("xfail")
        return "skipped", "xfail"
    skipped = [r for r in reps if r[1] == "skipped"]
    if skipped:
        if any(r[2] == "SubtestReport" for r in skipped):
            flags.add("subtest_skip")
        return "skipped", f"{skipped[0][0]} skipped"
    phases = {(r[0], r[1]) for r in reps if r[2] == "TestReport"}
    if {("setup", "passed"), ("call", "passed"), ("teardown", "passed")} <= phases:
        return "passed", None
    return "error", "incomplete reports"
