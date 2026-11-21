"""Mode 0: run exactly a commit's candidate tests on the patched code, in one fresh process.

Parent side (stdlib only, cheap to import: no unittest, no pytest):
    execute(spec, timeout)          write the spec, launch the runner, parse its results
    launch_argv(spec_path, ...)     the exact command line (one definition for selector and tools)
    parse_results(path, ids, rc)    conservative reading of a result file
    runner_digest(), source_digest(src), tests_identity(tests)
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

Result file (truncated at start, then one JSON object per line, written with O_APPEND):
    {"start": true, "schema": 1, "engine": ..., "pid": ..., "n": <number of tests>}
    {"id": ..., "outcome": ..., "cpu": s, "wall": s, "flags": [...], "detail": "..."?}   one per test
    {"done": true, "complete": bool, "n": <test lines written>, "counts": {...}, "cpu": s,
     "children_cpu": s, "startup_cpu": s, "wall": s, "engine": ..., "python": "3.13.x",
     "pytest": "9.2.0" | null, "aborted": null | "timeout" | "startup_timeout" | "error",
     "stopped": null | "<flag>", "flags": [...startup flags]}

outcome: passed | failed | error | skipped | timeout | notrun.
  failed  - a failure inside the test case run (setUp, body, subTest, tearDown, cleanups,
            unexpected success): pytest's call phase.
  error   - class/module fixture failure (setUpClass, tearDownClass, class cleanups,
            setUpModule, tearDownModule), collection/import error, id that does not resolve,
            or a feature this engine does not reproduce.
  skipped - unittest skip / SkipTest anywhere (also inside a subTest) and expected failures
            (pytest reports those as xfailed, i.e. skipped).
  timeout - exceeded its cap; every later test is notrun and the process exits (code 3).
A test's line is written once its outcome is final: after its own teardown and, when it is
the last selected test of its class/module, after that class/module teardown (pytest reports
a tearDownClass failure on the last test of the class).

flags: "spawn" (process creation: a child would import unpatched modules from disk),
"meta_path" (our overlay finder is no longer sys.meta_path[0]), "cwd" (cwd changed and not
restored), "read_patched" (opened a patched module's file from disk, e.g. inspect.getsource) -
these four are UNSAFE: such a pass is not evidence, and after meta_path/cwd every later test
is notrun.  Informational: "rtracker" (started multiprocessing's resource tracker, which runs
only stdlib code), "xfail", "subtest_skip", "pytest_import".

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
                          this run).
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
                          "read_shadowed", "sys_path", "write_pinned", "audit_error"})
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


def execute(spec, timeout, python=None, hash_seed=None, workdir=None, env=None, cpu_count=None):
    """Run one Mode 0 spec in a fresh process (own session; the whole group is killed on
    timeout and after exit). spec["result"] is set here. Returns a dict:
        outcomes   {id: outcome} from parse_results (only "passed" is evidence)
        returncode exit code (negative: signal), None if it could not be started
        cpu        user+sys of the child and its reaped descendants (wait4 rusage; an fs-mode
                   runner reaps its multiprocessing children before it exits)
        wall       seconds; timed_out bool; done the done line (dict) or None; lines {id: line}
    cpu_count: see launch_argv (None = unchanged launch).
    """
    import signal
    import subprocess
    import tempfile
    import threading

    ids = [t["id"] for t in spec["tests"]]
    fd, spec_path = tempfile.mkstemp(prefix="br-m0-", suffix=".json", dir=workdir)
    res_path = spec_path[:-5] + ".out"
    res = {"outcomes": {i: "notrun" for i in ids}, "returncode": None, "cpu": 0.0, "wall": 0.0,
           "timed_out": False, "done": None, "lines": {}}
    t0 = time.monotonic()
    try:
        spec = dict(spec, result=res_path)
        with os.fdopen(fd, "w") as fh:
            json.dump(spec, fh)
        argv, run_env = launch_argv(spec_path, python=python, hash_seed=hash_seed, env=env,
                                    cpu_count=cpu_count)
        try:
            proc = subprocess.Popen(argv, env=run_env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True, close_fds=True)
        except Exception:
            return res

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
            while True:
                try:
                    _, status, usage = os.wait4(proc.pid, 0)
                    break
                except InterruptedError:
                    continue
        finally:
            timer.cancel()
        proc.returncode = rc = os.waitstatus_to_exitcode(status)
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # leftovers (pool workers, trackers)
        except OSError:
            pass
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
        return res
    finally:
        res["wall"] = time.monotonic() - t0
        for p in (spec_path, res_path, res_path + ".mp"):
            try:
                os.remove(p)
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


def source_digest(src, dirs=("sqlglot", "tests")):
    """sha256 over the sorted relative file list and contents of src/sqlglot and src/tests
    (ignoring __pycache__ and .pyc)."""
    files = []
    for d in dirs:
        for root, subdirs, names in os.walk(os.path.join(src, d)):
            subdirs[:] = sorted(s for s in subdirs if s != "__pycache__")
            for name in names:
                if not name.endswith((".pyc", ".pyo")):
                    files.append(os.path.relpath(os.path.join(root, name), src).replace(os.sep, "/"))
    h = hashlib.sha256()
    for rel in sorted(files):
        with open(os.path.join(src, rel), "rb") as fh:
            data = fh.read()
        h.update(rel.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def tests_identity(tests):
    """sha256 over the ordered (number, id) pairs of a tests.json "tests" list (not durations)."""
    h = hashlib.sha256()
    for t in tests:
        h.update(f"{t['number']}\t{t['id']}\n".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------- child side

_times = os.times
_mono = time.monotonic


_process_time = time.process_time


def _cpu():
    """user+sys CPU of this process (all threads), high resolution."""
    return _process_time()


def _short(err, limit=300):
    try:
        text = f"{type(err).__name__}: {err}"
    except Exception:
        text = type(err).__name__
    return text[:limit]


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
        self.pytest_version = None
        self.extra = None  # fs mode: callable -> extra done-line fields
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
        obj = {"id": tid, "outcome": outcome, "cpu": round(cpu, 6), "wall": round(wall, 6),
               "flags": sorted(set(flags) | self.startup_flags)}
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
        """Write `rest` for every test without a line, then the done line, and close."""
        with self.lock:
            if self.closed:
                return False
            self.closed = True
            for tid in self.ids:
                self._test_line(tid, rest)
            self._done(complete, aborted, stopped)
            return True

    def abort_timeout(self, startup=False):
        """Called by the watchdog thread: the current test timed out."""
        if not self.lock.acquire(timeout=2.0):
            return
        try:
            if self.closed:
                return
            self.closed = True
            self.timed_out = True
            if self.current is not None and not startup:
                self._test_line(self.current, "timeout", flags=("timeout",))
            for tid in self.ids:
                self._test_line(tid, "notrun")
            self._done(False, aborted="startup_timeout" if startup else "timeout")
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
        self.startup = False
        self.before_exit = None  # fs mode: reap children before the timeout exit
        try:
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
        self.thread = threading.Thread(target=self._loop, name="br-mode0-watchdog", daemon=True)
        self.thread.start()

    def arm(self, tid, cap, startup=False):
        with self.cond:
            self.results.current = tid
            self.startup = startup
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
                startup = self.startup
                break
        self.results.abort_timeout(startup=startup)
        if self.before_exit is not None:
            try:
                self.before_exit()
            except BaseException:
                pass
        os._exit(EXIT_TIMEOUT)


class _Ctx:
    """Per-test observation: flags raised by the audit hook and the post-test checks."""

    def __init__(self, src, finder, patched_paths):
        self.src = src
        self.finder = finder
        self.flags = set()
        self.patched_paths = patched_paths
        self.patched_names = {os.path.basename(p) for p in patched_paths}

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
_RTRACKER_PREFIX = "from multiprocessing.resource_tracker import main;main("


def _is_resource_tracker(argv):
    """multiprocessing's resource tracker: `<python> [flags] -c 'from multiprocessing.
    resource_tracker import main;main(<fd>)'`. It imports only the stdlib."""
    argv = [os.fsdecode(a) for a in argv]
    if len(argv) < 3 or argv[-2] != "-c":
        return False
    cmd = argv[-1]
    return (cmd.startswith(_RTRACKER_PREFIX) and cmd.endswith(")")
            and cmd[len(_RTRACKER_PREFIX):-1].isdigit())


def _install_spawn_watch(ctx):
    def hook(event, args):
        try:
            if event in _SPAWN_EVENTS:
                ctx.flags.add("spawn")
            elif event == "open" and ctx.patched_names:
                path = args[0]
                if isinstance(path, int):
                    return
                path = os.fsdecode(path)
                if os.path.basename(path) in ctx.patched_names:
                    full = os.path.abspath(path)
                    if full in ctx.patched_paths or os.path.realpath(full) in ctx.patched_paths:
                        ctx.flags.add("read_patched")
        except Exception:
            pass

    sys.addaudithook(hook)

    # multiprocessing's spawn/forkserver start methods and its resource tracker go through
    # _posixsubprocess.fork_exec directly, which raises no audit event.
    try:
        import _posixsubprocess

        original = _posixsubprocess.fork_exec

        def fork_exec(*args, **kwargs):
            try:
                ctx.flags.add("rtracker" if _is_resource_tracker(args[0]) else "spawn")
            except Exception:
                ctx.flags.add("spawn")
            return original(*args, **kwargs)

        _posixsubprocess.fork_exec = fork_exec
    except Exception:
        pass


# ------------------------------------------------------------ filesystem overlay mode

_MP_SPAWN_PREFIX = "from multiprocessing.spawn import spawn_main; spawn_main("
_STICKY = frozenset({"read_shadowed", "spawn", "write_pinned", "audit_error"})
_CHILD_EVENT_FLAGS = {"read_shadowed": "read_shadowed", "spawn": "spawn", "bad": "spawn",
                      "write_pinned": "write_pinned", "audit_error": "audit_error"}
_WRITE_BITS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def _under(path, top):
    return path == top or path.startswith(top + os.sep)


class _Shadow:
    """Matches opens of shadowed originals and of the pinned bytecode of changed modules."""

    def __init__(self, paths):
        self.files, self.names, self.pyc = set(), set(), {}
        for p in paths:
            for q in {os.path.abspath(p), os.path.realpath(p)}:
                d, name = os.path.split(q)
                self.files.add(q)
                self.names.add(name)
                if name.endswith(".py"):
                    self.pyc.setdefault(name[:-3], set()).add(os.path.join(d, "__pycache__"))

    def hit(self, path):
        name = os.path.basename(path)
        if name in self.names:
            full = os.path.abspath(path)
            return full in self.files or os.path.realpath(full) in self.files
        if name.endswith(".pyc"):
            dirs = self.pyc.get(name.split(".", 1)[0])
            if dirs:
                full = os.path.abspath(path)
                return (os.path.dirname(full) in dirs
                        or os.path.dirname(os.path.realpath(full)) in dirs)
        return False


class _FsCtx(_Ctx):
    """fs mode: the Ctx of the runner (child=False) or of a multiprocessing child (True).

    In the runner, flags go to the running test; in a child they are written as events to the
    side file, which the runner reads in check_after()."""

    def __init__(self, root, pinned, shadowed, side, env_value, child=False, side_fd=None):
        super().__init__(root, None, set())
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
        except Exception:
            try:
                ctx.raise_flag("audit_error")
            except Exception:
                pass

    sys.addaudithook(hook)

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
            if _is_resource_tracker(args[0]):
                kind = "rtracker"
            elif not ctx.child and _is_mp_spawn(args) and ctx.state_problem(True) is None:
                kind = "mp_spawn"
            else:
                kind = "spawn"
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
    ctx.event({"ev": "start", "cwd": os.getcwd()[-200:]})


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
    return ids, caps


def run(spec, install_overlay):
    """Child entry point (called by replica_runner.py). Returns the exit code."""
    try:
        _redirect_stdio(spec.get("log"))
        ids, caps = _validate(spec)
        fs = _fs_config(spec)
    except Exception:
        return EXIT_BADSPEC
    results = _Results(spec["result"], ids, spec["engine"])
    ctx = None
    try:
        os.environ["SKIP_INTEGRATION"] = "0"
        for var in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DEBUG", "PYTEST_CURRENT_TEST"):
            os.environ.pop(var, None)
        os.environ.pop(FS_ENV, None)
        src = os.path.realpath(spec["src"])
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
            results.extra = lambda: {"overlay": "fs", "mp_children": ctx.mp_started,
                                     "child_events": ctx.events}
        watchdog = _Watchdog(results)
        if fs is not None:
            watchdog.before_exit = ctx.reap
        watchdog.arm(None, float(spec.get("startup_cap", 60.0)), startup=True)
        ctx.finder = install_overlay(src, spec["patched"])
        for d in spec.get("site", []):
            sys.path.append(d)
        if spec["engine"] == "unittest":
            code = _run_unittest(ids, caps, results, watchdog, ctx)
        else:
            code = _run_pytest(ids, caps, results, watchdog, ctx, src)
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


# ----------------------------------------------------------------- unittest engine

_PYTEST_ONLY_CLASS = ("setup_method", "teardown_method", "setup_class", "teardown_class",
                      "pytestmark")
_PYTEST_ONLY_MODULE = ("setup_module", "teardown_module", "pytestmark", "pytest_plugins")


class _Entry:
    __slots__ = ("id", "module", "cls", "method", "error")

    def __init__(self, tid):
        self.id, self.module, self.cls, self.method, self.error = tid, None, None, None, None


def _collect_unittest(ids, src):
    """Resolve ids like pytest would collect them; unresolvable or unsupported -> error."""
    import importlib
    import inspect
    import unittest
    import warnings

    loader = unittest.TestLoader()
    modules = {}
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
        modname = path[:-3].replace("/", ".")
        if modname not in modules:
            try:
                with warnings.catch_warnings(record=True):
                    _pytest_warning_filters(warnings)
                    modules[modname] = importlib.import_module(modname)
            except BaseException as error:  # noqa: BLE001 - pytest: collection error
                if not isinstance(error, Exception):
                    raise
                modules[modname] = error
        mod = modules[modname]
        if isinstance(mod, BaseException):
            e.error = "collect: " + _short(mod)
            continue
        try:
            if os.path.realpath(getattr(mod, "__file__", "") or "") != os.path.realpath(os.path.join(src, path)):
                e.error = "module file mismatch"
                continue
            if any(hasattr(mod, name) for name in _PYTEST_ONLY_MODULE):
                e.error = "module uses pytest-only features"
                continue
            cls = getattr(mod, cname, None)
            if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase)):
                e.error = "not a unittest.TestCase class"
                continue
            if inspect.isabstract(cls) or not getattr(cls, "__test__", True):
                e.error = "class not collected by pytest"
                continue
            if any(hasattr(cls, name) for name in _PYTEST_ONLY_CLASS):
                e.error = "class uses pytest-only features"
                continue
            if mname not in loader.getTestCaseNames(cls):
                e.error = "method not collected"
                continue
            func = getattr(cls, mname)
            if not getattr(func, "__test__", True) or hasattr(func, "pytestmark"):
                e.error = "method not collected / pytest marks"
                continue
        except Exception as error:
            e.error = "collect: " + _short(error)
            continue
        e.module, e.cls, e.method = mod, cls, mname
    return entries


def _pytest_warning_filters(warnings):
    # What pytest's warnings plugin does around collection and each test (no ini filters).
    if not sys.warnoptions:
        warnings.filterwarnings("always", category=DeprecationWarning)
        warnings.filterwarnings("always", category=PendingDeprecationWarning)


def _run_unittest(ids, caps, results, watchdog, ctx):
    import unittest
    import warnings
    from unittest.case import _SubTest

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

    valid = [e for e in entries if e.error is None]
    nxt = {id(a): b for a, b in zip(valid, valid[1:])}
    state = {"module": None, "module_status": None, "cls": None, "cls_status": None}

    def module_setup(mod):
        state["module"], state["module_status"] = mod, ("ok", None)
        fn = getattr(mod, "setUpModule", None)
        if fn is None:
            return
        try:
            fn()
        except unittest.SkipTest as error:
            state["module_status"] = ("skipped", "setUpModule skip: " + _short(error))
        except Exception as error:
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
            fn()
        except Exception as error:
            return "tearDownModule: " + _short(error)
        return None

    def class_setup(cls):
        state["cls"], state["cls_status"] = cls, ("ok", None)
        if getattr(cls, "__unittest_skip__", False):
            state["cls_status"] = ("skipped", "class skipped: " + str(getattr(cls, "__unittest_skip_why__", "")))
            return
        try:
            cls.setUpClass()
        except Exception as error:
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
            cls.tearDownClass()
        except Exception as error:
            problem = "tearDownClass: " + _short(error)
        finally:
            try:
                cls.doClassCleanups()
            except Exception as error:
                problem = problem or "doClassCleanups: " + _short(error)
            if getattr(cls, "tearDown_exceptions", None):
                problem = problem or "class cleanup: " + _short(cls.tearDown_exceptions[0][1])
        return problem

    for e in entries:
        if e.error is not None:
            results.emit(e.id, "error", detail=e.error)
            continue
        ctx.flags = flags = set()
        watchdog.arm(e.id, caps[e.id])
        cpu0, wall0 = _cpu(), _mono()
        outcome, detail = None, None
        try:
            if state["module"] is not e.module:
                module_setup(e.module)
            mstat = state["module_status"]
            if mstat[0] != "ok":
                outcome, detail = mstat
            else:
                if state["cls"] is not e.cls:
                    class_setup(e.cls)
                cstat = state["cls_status"]
                if cstat[0] != "ok":
                    outcome, detail = cstat
                else:
                    result = Result()
                    case = e.cls(e.method)
                    with warnings.catch_warnings(record=True):
                        _pytest_warning_filters(warnings)
                        case.run(result)
                    del case
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
            # Teardown that pytest attributes to this test (last selected of its class/module).
            following = nxt.get(id(e))
            if following is None or following.cls is not e.cls or following.module is not e.module:
                problem = class_teardown()
                if problem and outcome in ("passed", "skipped"):
                    outcome, detail = "error", problem
            if following is None or following.module is not e.module:
                problem = module_teardown()
                if problem and outcome in ("passed", "skipped"):
                    outcome, detail = "error", problem
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
        results.emit(e.id, outcome, _cpu() - cpu0, _mono() - wall0, flags, detail)
        stop = flags & STOP_FLAGS
        if stop:
            results.finish(stopped=sorted(stop)[0])
            return EXIT_OK
    results.finish()
    return EXIT_OK


# ------------------------------------------------------------------- pytest engine

def _run_pytest(ids, caps, results, watchdog, ctx, src):
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    import pytest

    results.pytest_version = pytest.__version__
    wanted = set(ids)
    reports = {}
    collect_errors = set()
    state = {"started": False, "stop": None}

    class Plugin:
        @pytest.hookimpl(tryfirst=True)
        def pytest_collectreport(self, report):
            if report.failed:
                collect_errors.add(report.nodeid.split("::")[0])

        @pytest.hookimpl(trylast=True)
        def pytest_collection_modifyitems(self, session, config, items):
            # Whole files are collected; keep exactly the requested items, once each, in the
            # requested order (pytest's class/module fixture scoping follows this sequence).
            keep, seen = [], set()
            for item in items:
                if item.nodeid in wanted and item.nodeid not in seen:
                    seen.add(item.nodeid)
                    keep.append(item)
            keep.sort(key=lambda item: order[item.nodeid])
            items[:] = keep

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            tid = item.nodeid
            if not state["started"]:
                state["started"] = True
                results.startup_flags |= ctx.flags
                results.startup_cpu = _cpu()
                stop = ctx.check_after()
                results.startup_flags |= stop
                stop &= STOP_FLAGS
                if stop:  # collection displaced the overlay: nothing here is evidence
                    state["stop"] = sorted(stop)[0]
                    item.session.shouldstop = "br-mode0: " + state["stop"]
            ctx.flags = flags = set()
            reports[tid] = []
            watchdog.arm(tid, caps[tid])
            cpu0, wall0 = _cpu(), _mono()
            try:
                return (yield)
            finally:
                watchdog.disarm()
                cpu, wall = _cpu() - cpu0, _mono() - wall0
                flags |= ctx.check_after()
                outcome, detail = _pytest_outcome(reports.get(tid, []), flags)
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
        ret = pytest.main(args, plugins=[Plugin()])
    except BaseException as error:  # noqa: BLE001
        results.finish(complete=False, aborted="error: pytest " + _short(error))
        return EXIT_ABORT
    watchdog.disarm()
    code = int(ret)
    if results.startup_cpu is None:
        results.startup_cpu = _cpu()
    with results.lock:
        rest_error = {tid for tid in ids if tid.split("::")[0] in collect_errors}
    for tid in ids:
        if tid in rest_error:
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
