"""Import-state diff: which top-level "cells" of sqlglot's import-time state does a patch change?

Parent side (imported by the selector):
    run(patched_files, src_root, modules, baseline_path, timeout=30.0) -> StateDiff
    load_baseline(path) / snapshot(src_root, modules, patched_files=None) / diff(base, new)
Child side (this file as a script, never imported by it):
    python -I -S state_probe.py <spec.json>

Protocol (that of selector/import_probe.py, whose clean-import guarantee this keeps): a fresh
`python -I -S` child, the patched modules served from memory by a meta-path finder, cwd = the
source root, `sqlglot` imported first and then every listed module in order; any exception
(SystemExit included) is a failure. The child deletes its spec (which holds a random nonce and
the result path) before importing anything. Only after the imports AND the snapshot succeed does
it create the result file exclusively and write b"OK <nonce>\\n" followed by the marshalled
snapshot. The parent requires exit status 0, that exact header and a snapshot that unmarshals;
stdout/stderr go to /dev/null and are never parsed.

Cells. ("g", module, name) for every global of every module named `sqlglot` or `sqlglot.*` in
sys.modules after the imports; ("c", "<module>:<Qual.Name>", attr) for every entry of vars(cls)
of every class whose __module__ is `sqlglot`/`sqlglot.*` (same key format as maps.class_info),
reached through module globals, nested class attributes, the __subclasses__() closure and any
value the deep walk meets. Two synthetic class cells: "__bases__" (the bases, by name) and
"__class__" (the metaclass, by name), since neither lives in vars(cls).
Skipped, provably irrelevant:
  module `__builtins__` (the builtins module's dict, compared separately in the environment),
  `__loader__` and `__spec__` (the probe's overlay loader differs from the normal loader by
  construction; at test time both sides use the normal loader);
  class `__module__`, `__qualname__` (already the cell key), `__firstlineno__` (line numbers),
  `__static_attributes__` (derived from method code, which is fingerprinted), `_abc_impl` (C
  ABC cache; registrations are not modelled, see the soundness notes), and the `__dict__` /
  `__weakref__` getset descriptors (no state).
Annotations are their own cell `__annotations__` (on 3.14, the class's `__annotate_func__` /
`__annotate__` / `__annotations_cache__` entries are folded into that one cell), so the caller
can treat annotation-only edits specially.

Each cell value gets three deep fingerprints (16-byte blake2b, or the raw leaf token for short
primitives): h (dicts and sets order-insensitive), o (dict insertion order included at every
level; sets stay sorted because their order is hash-seed dependent) and n (like h, with every
code object masked: equal n and different h means "only code objects differ"). Containers
(dict / set / frozenset / list / tuple) also get a shallow breakdown: per-key (dict) or
per-element (set, seq) entries (key_fp, value h[:8], value n[:8] or b"" when equal to h).
Function-like values (function, method, staticmethod, classmethod, property, partial,
lru_cache wrapper) get component hashes code / defaults / closure / other plus first line and
qualname (metadata only, not in the fingerprint).

Deep fingerprint rules: primitives by type+value; classes and modules by name (their contents
are their own cells); functions by qualname, module, code (co_code, consts recursively incl.
nested code, names, varnames, freevars, cellvars, arg counts, flags, nlocals, stacksize,
exception table; NOT filename or line table), defaults, kwdefaults, closure cell contents,
__dict__, __doc__, annotations, type params; containers recursively; instances by class +
__dict__ + __slots__ + builtin-base payload (dict/list/set/str/int... subclasses), with cycle
protection (a back-reference marker; objects whose subtree hit a back-reference are never
memoised, so every hash is independent of walk order); re.Pattern by pattern+flags;
lru_cache wrappers by wrapped function + cache_info() (currsize > 0 -> `warm_caches`);
anything else is opaque: its repr when that has no memory address (`repr_types`, informative),
else its type name only, and the location goes into `opaque`.

Environment (outside the module graph): recorded as what the import changed (before vs after)
where the absolute value depends on the host (environ, sys.path, cwd, umask, locale, signal
handlers, recursion limit, builtins rebinding, random state, int_max_str_digits, gc thresholds,
time.tzname), and as absolute values where the default is fixed by the interpreter (decimal
context, logging, warnings.filters, sys hooks, atexit count, threads, sys.flags). Plus a
shallow fingerprint of every non-sqlglot module in sys.modules (global name -> type and, for
classes/functions/builtins, module.qualname; the same for the attributes of classes defined
there), so stdlib monkeypatching (`json.dumps = ...`) shows up as `module:<name>`.
"""

import base64
import json
import marshal
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FORMAT = 1
HEADER_LIMIT = 64


def module_name(path):
    """sqlglot/dialects/duckdb.py -> sqlglot.dialects.duckdb; a package's __init__.py -> the package."""
    name = path[:-3].replace("/", ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            proc.kill()
        except OSError:
            pass


class StateDiff:
    def __init__(self, ok, why, cells=None, env_changed=(), opaque=(), warm_caches=(), new_modules=(),
                 lost_modules=(), unstable=(), stats=None):
        self.ok = ok
        self.why = why
        self.cells = cells if cells is not None else {}
        self.env_changed = list(env_changed)
        self.opaque = list(opaque)
        self.warm_caches = list(warm_caches)
        self.new_modules = list(new_modules)
        self.lost_modules = list(lost_modules)
        self.unstable = list(unstable)
        self.stats = stats or {}

    def __repr__(self):
        return (f"StateDiff(ok={self.ok}, why={self.why!r}, cells={len(self.cells)}, env={self.env_changed}, "
                f"opaque={len(self.opaque)}, warm={self.warm_caches}, new={self.new_modules}, "
                f"lost={self.lost_modules})")


def snapshot_detailed(src_root, modules, patched_files=None, timeout=30.0, python=sys.executable):
    """(snapshot dict or None, why). One fresh child: overlay, import, snapshot, result file."""
    tmp = None
    proc = None
    patched_files = patched_files or {}
    try:
        tmp = tempfile.mkdtemp(prefix="br-state-")
        spec_path = os.path.join(tmp, "spec.json")
        result_path = os.path.join(tmp, "result")
        nonce = secrets.token_hex(16)
        src = os.path.abspath(src_root)
        spec = {
            "src": src,
            "patched": {module_name(p): [os.path.join(src, p), base64.b64encode(s).decode()]
                        for p, s in patched_files.items()},
            "modules": list(modules),
            "nonce": nonce,
            "result": result_path,
            "keyfp": os.path.join(HERE, "keyfp.py"),
        }
        with open(spec_path, "w") as fh:
            json.dump(spec, fh)
        proc = subprocess.Popen([python, "-I", "-S", os.path.join(HERE, "state_probe.py"), spec_path],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                cwd=tmp, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            proc.wait()
            return None, f"timeout after {timeout}s"
        try:
            with open(result_path, "rb") as fh:
                got = fh.read()
        except OSError:
            return None, f"exit {code}, no result"
        header = f"OK {nonce}\n".encode()
        if code != 0 or not got.startswith(header):
            return None, f"exit {code}: {got[:300].decode(errors='replace').strip()}"
        try:
            snap = marshal.loads(got[len(header):])
        except Exception as error:  # noqa: BLE001
            return None, f"bad snapshot {error!r}"
        if not isinstance(snap, dict) or snap.get("format") != FORMAT or snap.get("nonce") != nonce:
            return None, "bad snapshot"
        return snap, "ok"
    except Exception as error:  # noqa: BLE001 - a probe that cannot run proves nothing
        return None, f"probe error {error!r}"
    finally:
        if proc is not None:
            if proc.poll() is None:
                _kill(proc)
                proc.wait()
            else:
                _kill(proc)
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def snapshot(src_root, modules, patched_files=None, timeout=30.0, python=sys.executable):
    return snapshot_detailed(src_root, modules, patched_files, timeout=timeout, python=python)[0]


# --- baseline --------------------------------------------------------------------------------

def build_baseline(src_root, modules, runs=3, timeout=60.0, python=sys.executable):
    """Snapshot the unpatched tree `runs` times (each child has its own random hash seed) and
    record what is not reproducible: cells whose order hash varies (`unstable_order`, compared
    without order afterwards), and cells / env items / module fingerprints whose content varies
    (`unstable`, always reported as differing)."""
    snaps = []
    for _ in range(runs):
        snap, why = snapshot_detailed(src_root, modules, timeout=timeout, python=python)
        if snap is None:
            raise RuntimeError(f"baseline snapshot failed: {why}")
        snaps.append(snap)
    base = snaps[0]
    unstable, unstable_order = set(), set()
    for other in snaps[1:]:
        for key in set(base["cells"]) | set(other["cells"]):
            a, b = base["cells"].get(key), other["cells"].get(key)
            if a is not None and b is not None and a[3] == b[3] == "seq" and a[0] != b[0] \
                    and _sorted_items(a) == _sorted_items(b) and all(k is not None for k, _, _ in a[4]):
                unstable_order.add(key)  # e.g. tuple(some_set): membership is reproducible, order is not
            elif a is None or b is None or a[0] != b[0] or a[2] != b[2] or _sorted_items(a) != _sorted_items(b):
                unstable.add(key)
            elif a[1] != b[1] or a[4] != b[4]:
                unstable_order.add(key)
        for section in ("env", "mods"):
            for key in set(base[section]) | set(other[section]):
                if base[section].get(key) != other[section].get(key):
                    unstable.add((section, key))
        for field in ("sqlglot_modules", "opaque", "warm"):
            if sorted(base[field]) != sorted(other[field]):
                unstable.add(("field", field))
    base = dict(base)
    base.pop("nonce", None)
    base["unstable"] = sorted(unstable, key=repr)
    base["unstable_order"] = sorted(unstable_order, key=repr)
    base["modules"] = list(modules)
    base["python"] = sys.version
    base["runs"] = runs
    return base


def write_baseline(base, path):
    data = zlib.compress(marshal.dumps(base), 9)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return len(data)


def load_baseline(path):
    with open(path, "rb") as fh:
        base = marshal.loads(zlib.decompress(fh.read()))
    if base.get("format") != FORMAT:
        raise ValueError("baseline format mismatch")
    return base


# --- diff -----------------------------------------------------------------------------------

def _sorted_items(rec):
    items = rec[4]
    if items is None:
        return None
    return sorted(items, key=repr)


def _cell_detail(a, b, order_stable):
    """Detail for a cell whose records differ; None when they do not (as compared)."""
    ha, oa, na, ka, ia, fa = a
    hb, ob, nb, kb, ib, fb = b
    if ha == hb and (oa == ob or not order_stable):
        return None  # equal deep content (and order, where order is reproducible)
    if not order_stable and ka == kb == "seq" and _sorted_items(a) == _sorted_items(b):
        return None  # a sequence whose order is hash-seed dependent at baseline: same members
    detail = {"kind": kb, "keys": None, "order": False, "len": False, "code_only": ha != hb and na == nb}
    if ka != kb:
        detail["kind_changed"] = [ka, kb]
        return detail
    if kb == "func":
        detail["code"], detail["defaults"], detail["closure"], detail["other"] = (
            fa[i] != fb[i] for i in range(4))
        detail["first_line"], detail["qualname"] = fb[4], fb[5]
        detail["order"] = ha == hb and oa != ob and order_stable
        return detail
    if kb == "other" or ia is None or ib is None:
        detail["order"] = ha == hb and oa != ob and order_stable
        return detail
    detail["len"] = len(ia) != len(ib)
    attributable = all(k is not None for k, _, _ in ia) and all(k is not None for k, _, _ in ib)
    if kb == "dict":
        da = {k: (v, n) for k, v, n in ia}
        db = {k: (v, n) for k, v, n in ib}
        common_a = [k for k, _, _ in ia if k in db]
        common_b = [k for k, _, _ in ib if k in da]
        detail["order"] = order_stable and common_a != common_b
        if not attributable or len(da) != len(ia) or len(db) != len(ib):
            return detail
        keys = sorted(k for k in set(da) | set(db) if da.get(k, (None,))[0] != db.get(k, (None,))[0])
        code_only = sorted(k for k in keys if k in da and k in db and (da[k][1] or da[k][0]) == (db[k][1] or db[k][0]))
        if not keys and ha != hb:
            return detail  # equal shallow view, different deep fingerprint: not attributable
        if not keys and not detail["order"]:
            detail["order"] = True  # only nested order differs
            return detail
        detail["keys"] = keys
        detail["code_only_keys"] = code_only
        return detail
    # set / seq: membership by key_fp
    counts_a, counts_b = {}, {}
    for k, v, n in ia:
        counts_a.setdefault(k, []).append((v, n))
    for k, v, n in ib:
        counts_b.setdefault(k, []).append((v, n))
    dups = any(len(x) > 1 for x in counts_a.values()) or any(len(x) > 1 for x in counts_b.values())
    if not attributable or dups:
        if kb == "seq":
            detail["order"] = ha == hb and oa != ob and order_stable
        return detail
    keys = sorted(k for k in set(counts_a) | set(counts_b) if counts_a.get(k, [(None,)])[0][0] !=
                  counts_b.get(k, [(None,)])[0][0])
    if kb == "seq":
        common_a = [k for k, _, _ in ia if k in counts_b]
        common_b = [k for k, _, _ in ib if k in counts_a]
        detail["order"] = common_a != common_b or (ha == hb and oa != ob and order_stable)
        if detail["order"]:
            return detail  # order changes of a sequence are not attributable to keys
    if not keys:
        if kb == "set" and ha == hb:
            return None  # equal sets: o of a set is order-insensitive anyway
        return detail
    detail["keys"] = keys
    detail["code_only_keys"] = sorted(
        k for k in keys if k in counts_a and k in counts_b
        and (counts_a[k][0][1] or counts_a[k][0][0]) == (counts_b[k][0][1] or counts_b[k][0][0]))
    return detail


def diff(base, new):
    """Compare a baseline (load_baseline) with a patched snapshot. -> StateDiff (ok=True)."""
    unstable = set(base.get("unstable", ()))
    unstable_order = set(base.get("unstable_order", ()))
    cells = {}
    bc, nc = base["cells"], new["cells"]
    for key in set(bc) | set(nc):
        a, b = bc.get(key), nc.get(key)
        if a is None or b is None:
            rec = a or b
            cells[key] = {"kind": rec[3], "keys": None, "order": False, "len": True, "code_only": False,
                          "added" if a is None else "removed": True}
            continue
        if key in unstable:
            cells[key] = {"kind": b[3], "keys": None, "order": False, "len": False, "code_only": False,
                          "unstable": True}
            continue
        detail = _cell_detail(a, b, key not in unstable_order)
        if detail is not None:
            cells[key] = detail
    env_changed = []
    for key in sorted(set(base["env"]) | set(new["env"])):
        if base["env"].get(key) != new["env"].get(key) or ("env", key) in unstable:
            env_changed.append(key)
    for key in sorted(set(base["mods"]) | set(new["mods"])):
        if base["mods"].get(key) != new["mods"].get(key) or ("mods", key) in unstable:
            env_changed.append("module:" + key)
    old_mods, new_mods = set(base["sqlglot_modules"]), set(new["sqlglot_modules"])
    return StateDiff(True, "compared", cells=cells, env_changed=env_changed, opaque=new["opaque"],
                     warm_caches=new["warm"], new_modules=sorted(new_mods - old_mods),
                     lost_modules=sorted(old_mods - new_mods),
                     unstable=[k for k in unstable if k in cells], stats=new.get("stats"))


def run(patched_files, src_root, modules, baseline_path, timeout=30.0, python=sys.executable, baseline=None):
    """Import `sqlglot` and every module in `modules` with `patched_files` ({repo path: new
    source bytes}) overlaid in a fresh interpreter, snapshot, and compare with the baseline.
    ok is False (and nothing else is meaningful) unless the import was clean, the snapshot
    completed and the comparison ran."""
    try:
        base = baseline if baseline is not None else load_baseline(baseline_path)
    except Exception as error:  # noqa: BLE001
        return StateDiff(False, f"baseline unavailable: {error!r}")
    if list(base.get("modules", ())) != list(modules):
        return StateDiff(False, "module list differs from the baseline's")
    if base.get("python") != sys.version and python == sys.executable:
        return StateDiff(False, "baseline built by another Python")
    new, why = snapshot_detailed(src_root, modules, patched_files, timeout=timeout, python=python)
    if new is None:
        return StateDiff(False, why)
    try:
        return diff(base, new)
    except Exception as error:  # noqa: BLE001
        return StateDiff(False, f"diff error {error!r}")


# --- child ---------------------------------------------------------------------------------

def _child(spec_path):
    import importlib
    import importlib.abc
    import importlib.machinery
    import importlib.util

    t0 = time.process_time()
    w0 = time.perf_counter()
    sys.dont_write_bytecode = True
    with open(spec_path) as fh:
        spec = json.load(fh)
    os.remove(spec_path)
    nonce, result = spec.pop("nonce"), spec["result"]

    def report(data):
        fd = os.open(result, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)

    try:
        kspec = importlib.util.spec_from_file_location("_br_keyfp", spec["keyfp"])
        keyfp = importlib.util.module_from_spec(kspec)
        kspec.loader.exec_module(keyfp)
        patched = {name: (path, base64.b64decode(src)) for name, (path, src) in spec["patched"].items()}

        class Loader(importlib.machinery.SourceFileLoader):
            def get_code(self, fullname):
                return compile(patched[fullname][1], self.path, "exec", dont_inherit=True)

            def get_source(self, fullname):
                return importlib.util.decode_source(patched[fullname][1])

        class Finder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname not in patched:
                    return None
                origin = patched[fullname][0]
                search = [os.path.dirname(origin)] if origin.endswith("__init__.py") else None
                return importlib.util.spec_from_file_location(fullname, origin, loader=Loader(fullname, origin),
                                                              submodule_search_locations=search)

        sys.meta_path.insert(0, Finder())
        sys.path.insert(0, spec["src"])
        os.chdir(spec["src"])
        limit = max(200, sys.getrecursionlimit() - 250)
        sys.setrecursionlimit(limit)
        # Taken after the probe's own setup (path, cwd, finder, limit), so every "=" below
        # means "the import left it as the probe set it".
        env_before = _env_raw()
        modules_before = set(sys.modules)
        importlib.import_module("sqlglot")
        for name in spec["modules"]:
            importlib.import_module(name)
        for name in patched:
            mod = sys.modules.get(name)
            if mod is not None and not isinstance(getattr(getattr(mod, "__spec__", None), "loader", None), Loader):
                raise ImportError(f"{name} was not loaded from the overlay")
        t_import = time.process_time() - t0
        env_after = _env_raw()
        modules_after = set(sys.modules)
        sys.setrecursionlimit(max(limit, 20000))  # the walk recurses; the import ran with the lowered limit
        snap = _snapshot(keyfp, env_before, env_after, modules_before, modules_after)
        snap["nonce"] = nonce
        snap["stats"]["import_cpu"] = t_import
        snap["stats"]["total_cpu"] = time.process_time() - t0
        snap["stats"]["total_wall"] = time.perf_counter() - w0
        data = marshal.dumps(snap)
    except BaseException as error:  # noqa: BLE001 - SystemExit and KeyboardInterrupt included
        try:
            report(f"FAIL {type(error).__name__}: {error!r}"[:300].encode(errors="replace"))
        except BaseException:  # noqa: BLE001
            pass
        os._exit(1)
    report(f"OK {nonce}\n".encode() + data)
    os._exit(0)


_COMMON_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP", "SIGPIPE", "SIGCHLD", "SIGALRM", "SIGUSR1", "SIGUSR2", "SIGQUIT")


def _name_of(obj):
    if obj is None:
        return "None"
    mod = getattr(obj, "__module__", None)
    qual = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None)
    if isinstance(mod, str) and isinstance(qual, str):
        return f"{mod}.{qual}"
    return "<" + type(obj).__name__ + ">"


def _env_raw():
    """Environment state as plain comparable values (taken before and after the import)."""
    import builtins
    import gc

    env = {}
    env["environ"] = dict(os.environ)
    env["sys.path"] = list(sys.path)
    env["meta_path"] = [type(f).__module__ + "." + type(f).__qualname__ for f in sys.meta_path]
    env["path_hooks"] = [_name_of(h) for h in sys.path_hooks]
    env["cwd"] = os.getcwd()
    env["builtins"] = {k: id(v) for k, v in vars(builtins).items()}
    env["recursionlimit"] = sys.getrecursionlimit()
    try:
        env["int_max_str_digits"] = sys.get_int_max_str_digits()
    except AttributeError:
        pass
    env["gc"] = (gc.get_threshold(), gc.isenabled())
    env["excepthook"] = _name_of(sys.excepthook)
    env["displayhook"] = _name_of(sys.displayhook)
    env["unraisablehook"] = _name_of(getattr(sys, "unraisablehook", None))
    env["breakpointhook"] = _name_of(getattr(sys, "breakpointhook", None))
    env["trace"] = sys.gettrace() is None
    env["profile"] = sys.getprofile() is None
    env["flags"] = repr(sys.flags)
    env["dont_write_bytecode"] = sys.dont_write_bytecode
    env["signals"] = {name: _name_of(signal.getsignal(getattr(signal, name)))
                      for name in _COMMON_SIGNALS if hasattr(signal, name)}
    try:
        import atexit

        env["atexit"] = atexit._ncallbacks()
    except Exception:  # noqa: BLE001
        env["atexit"] = None
    env["tzname"] = tuple(time.tzname)
    env["timezone"] = time.timezone
    mask = os.umask(0)
    os.umask(mask)
    env["umask"] = mask
    try:
        import _locale

        env["locale"] = _locale.setlocale(_locale.LC_ALL, None)
    except Exception:  # noqa: BLE001
        env["locale"] = None
    rnd = sys.modules.get("random")
    env["random"] = hash(repr(rnd.getstate())) if rnd is not None else None  # in-process comparison only
    env["stdio"] = [_name_of(type(s)) for s in (sys.stdin, sys.stdout, sys.stderr)]
    th = sys.modules.get("threading")
    env["threads"] = th.active_count() if th is not None else 1
    dec = sys.modules.get("decimal")
    env["decimal"] = repr(dec.getcontext()) if dec is not None else None
    wa = sys.modules.get("warnings")
    env["warnings"] = [repr((f[0], getattr(f[1], "pattern", f[1]), _name_of(f[2]), getattr(f[3], "pattern", f[3]),
                             f[4])) for f in wa.filters] if wa is not None else None
    lg = sys.modules.get("logging")
    if lg is not None:
        root = lg.root
        env["logging"] = (root.level, [_name_of(type(h)) for h in root.handlers], lg.Logger.manager.disable,
                          lg.getLoggerClass().__qualname__, _name_of(lg.getLogRecordFactory()))
        env["loggers"] = sorted(
            (name, getattr(lgr, "level", None), [_name_of(type(h)) for h in getattr(lgr, "handlers", ())],
             getattr(lgr, "propagate", None), getattr(lgr, "disabled", None))
            for name, lgr in list(lg.Logger.manager.loggerDict.items()))
    else:
        env["logging"] = None
        env["loggers"] = []
    return env


def _env_section(before, after, modules_before, modules_after):
    """What the import changed, in a form that does not depend on the host's starting state."""
    env = {}
    for key in after:
        a, b = before.get(key), after[key]
        if key == "environ":
            env[key] = sorted((k, v) for k, v in b.items() if a.get(k) != v) + sorted(
                (k, None) for k in a if k not in b)
        elif key == "builtins":
            env[key] = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        elif key == "random":
            env[key] = a != b
        elif key in ("decimal", "warnings", "logging", "loggers", "flags", "atexit", "threads", "excepthook",
                     "displayhook", "unraisablehook", "breakpointhook", "trace", "profile", "meta_path",
                     "path_hooks", "stdio", "dont_write_bytecode"):
            env[key] = b  # absolute: the interpreter fixes the default
        else:
            env[key] = "=" if a == b else b
    env["new_modules"] = sorted(m for m in modules_after - modules_before
                                if m != "sqlglot" and not m.startswith("sqlglot."))
    env["lost_modules"] = sorted(modules_before - modules_after)
    return {k: _plain(v) for k, v in env.items()}


def _plain(v):
    """Make a value marshal-able and comparable (tuples/lists/dicts of primitives)."""
    if isinstance(v, (str, int, float, bool, bytes)) or v is None:
        return v
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return tuple(_plain(x) for x in v)
    return repr(v)


def _snapshot(keyfp, env_before, env_after, modules_before, modules_after):
    import types

    walker = _Walker(keyfp)
    mods = sorted(n for n in modules_after if n == "sqlglot" or n.startswith("sqlglot."))
    cells = {}
    for name in mods:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        d = vars(mod)
        for gname in sorted(d, key=_sort_key):
            if gname in _SKIP_GLOBALS:
                continue
            walker.cell = ("g", name, gname)
            cells[walker.cell] = walker.record(d[gname])
        for v in list(d.values()):
            if isinstance(v, type):
                walker.add_class(v)
    done = set()
    per_class = []  # (cls, provisional key, [(attr, record)])
    while walker.pending:
        batch = sorted(walker.pending.values(), key=lambda c: (_class_key(c), id(c)))
        walker.pending = {}
        for cls in batch:
            if id(cls) in done:
                continue
            done.add(id(cls))
            key = _class_key(cls)
            recs = []
            d = cls.__dict__
            ann = []
            for attr in sorted(d, key=_sort_key):
                if attr in _SKIP_CLASS:
                    continue
                v = d[attr]
                if attr in ("__dict__", "__weakref__") and type(v).__name__ == "getset_descriptor":
                    continue
                if attr in _ANNOTATION_ATTRS:
                    ann.append((attr, v))
                    continue
                walker.cell = ("c", key, attr)
                recs.append((attr, walker.record(v)))
            if ann:
                walker.cell = ("c", key, "__annotations__")
                recs.append(("__annotations__", walker.record(
                    ann[0][1] if len(ann) == 1 and ann[0][0] == "__annotations__" else tuple(ann))))
            walker.cell = ("c", key, "__bases__")
            recs.append(("__bases__", walker.record(tuple(cls.__bases__))))
            walker.cell = ("c", key, "__class__")
            recs.append(("__class__", walker.record(type(cls))))
            per_class.append((cls, key, recs))
            try:
                subs = type.__subclasses__(cls)
            except TypeError:
                subs = []
            for sub in subs:
                walker.add_class(sub)
    # Several classes can share one module:qualname (sqlglot's Dialect metaclass creates a
    # `type("Tokenizer", ...)` per dialect). Name each of them by its anchor: the smallest
    # cell (as "kind:owner:attr") whose value is that class itself, then by content.
    groups = {}
    for entry in per_class:
        groups.setdefault(entry[1], []).append(entry)
    dups = []
    for key, entries in groups.items():
        if len(entries) == 1:
            for attr, rec in entries[0][2]:
                cells[("c", key, attr)] = rec
            continue
        dups.append(key)
        named = []
        for cls, _, recs in entries:
            anchor = min(walker.anchors.get(id(cls), ["?"]))
            content = walker.H(b"cls", [r[0] for _, r in recs])
            named.append((anchor, content, recs))
        named.sort(key=lambda e: (e[0], e[1]))
        used = set()
        for anchor, _, recs in named:
            name = f"{key}@{anchor}"
            n = 2
            while name in used:
                name = f"{key}@{anchor}#{n}"
                n += 1
            used.add(name)
            for attr, rec in recs:
                cells[("c", name, attr)] = rec
    del types
    snap = {
        "format": FORMAT,
        "cells": cells,
        "opaque": sorted(set(walker.opaque)),
        "repr_types": sorted(set(walker.repr_types)),
        "warm": sorted(set(walker.warm)),
        "dups": sorted(set(dups)),
        "sqlglot_modules": mods,
        "env": _env_section(env_before, env_after, modules_before, modules_after),
        "mods": _module_shallow(modules_after),
        "stats": {"nodes": walker.nodes, "memo": len(walker.memo), "cells": len(cells)},
    }
    return snap


_SKIP_GLOBALS = frozenset(("__builtins__", "__loader__", "__spec__"))
_SKIP_CLASS = frozenset(("__module__", "__qualname__", "__firstlineno__", "__static_attributes__", "_abc_impl"))
_ANNOTATION_ATTRS = frozenset(("__annotations__", "__annotate__", "__annotate_func__", "__annotations_cache__"))


def _sort_key(k):
    return (0, k) if isinstance(k, str) else (1, repr(k))


def _class_key(cls):
    return f"{cls.__module__}:{cls.__qualname__}"


def _is_sqlglot_name(mod):
    return isinstance(mod, str) and (mod == "sqlglot" or mod.startswith("sqlglot."))


def _module_shallow(modules_after):
    """Non-sqlglot modules: global name -> type (+ module.qualname for classes, functions,
    builtins), and the same for attributes of classes defined in that module. One hash per module."""
    import hashlib
    import types

    named = (type, types.FunctionType, types.BuiltinFunctionType, types.MethodType, types.ModuleType)

    def tag(v):
        t = type(v)
        s = t.__module__ + "." + t.__qualname__
        if isinstance(v, named):
            try:
                if isinstance(v, types.ModuleType):
                    s += "|" + str(v.__name__)
                else:
                    s += "|" + str(v.__module__) + "." + str(v.__qualname__)
            except Exception:  # noqa: BLE001
                s += "|?"
        return s

    out = {}
    for name in sorted(modules_after):
        if name == "sqlglot" or name.startswith("sqlglot.") or name == "__main__":
            continue  # __main__ is this script
        mod = sys.modules.get(name)
        if mod is None:
            continue
        try:
            d = vars(mod)
        except TypeError:
            continue
        parts = []
        for k in sorted(d, key=_sort_key):
            v = d[k]
            parts.append(f"{k}={tag(v)}")
            if isinstance(v, type) and getattr(v, "__module__", None) == name:
                try:
                    cd = v.__dict__
                    for a in sorted(cd, key=_sort_key):
                        if a in ("_abc_impl",):
                            continue
                        parts.append(f"{k}.{a}={tag(cd[a])}")
                except Exception:  # noqa: BLE001
                    parts.append(f"{k}.?")
        out[name] = hashlib.blake2b("\n".join(parts).encode("utf-8", "surrogatepass"), digest_size=12).digest()
    return out


class _Walker:
    """Deep fingerprints (h, o, n) of values, see the module docstring."""

    def __init__(self, keyfp):
        import functools
        import hashlib
        import re
        import types
        import weakref

        self.key_fp = keyfp.key_fp
        self.blake = hashlib.blake2b
        self.memo = {}
        self.kmemo = {}
        self.stack = {}
        self.depth = 0
        self.backrefs = 0
        self.nodes = 0
        self.opaque = []
        self.repr_types = []
        self.warm = []
        self.pending = {}
        self.anchors = {}
        self.seen_classes = set()
        self.cell = None
        self.types = types
        self.lru_type = type(functools.lru_cache(maxsize=None)(lambda: None))
        self.partial_type = functools.partial
        self.pattern_type = re.Pattern
        self.addr = re.compile(r"0x[0-9a-fA-F]{4,}")
        self.weakref_type = weakref.ref
        self.dispatch = {
            type(None): self._leaf, bool: self._leaf, int: self._leaf, float: self._leaf, str: self._leaf,
            bytes: self._leaf, complex: self._leaf,
            tuple: self._seq, list: self._seq, dict: self._dict, set: self._set, frozenset: self._set,
            types.FunctionType: self._function, types.CodeType: self._code, types.ModuleType: self._module,
            types.BuiltinFunctionType: self._builtin, types.MethodType: self._method,
            staticmethod: self._wrapper, classmethod: self._wrapper, property: self._property,
            functools.partial: self._partial, re.Pattern: self._pattern, types.MappingProxyType: self._mapping,
            type(Ellipsis): self._reprleaf, type(NotImplemented): self._reprleaf, range: self._reprleaf,
            slice: self._reprleaf, types.CellType: self._cellobj,
            self.lru_type: self._lru,
        }
        import _thread

        for t in (type(_thread.allocate_lock()), getattr(_thread, "RLock", None)):
            if t is not None:
                self.dispatch[t] = self._lock
        for t in (types.MethodDescriptorType, types.WrapperDescriptorType, types.GetSetDescriptorType,
                  types.MemberDescriptorType, types.ClassMethodDescriptorType, types.MethodWrapperType):
            self.dispatch[t] = self._descriptor

    # -- plumbing ------------------------------------------------------------------------
    def H(self, tag, parts):
        m = self.blake(tag, digest_size=16)
        for p in parts:
            m.update(len(p).to_bytes(4, "little"))
            m.update(p)
        return m.digest()

    def combine(self, tag, triples, otag=None, ordered=None):
        """Hash a node from its children's (h, o, n). `ordered`: alternative child list for o."""
        hs = [t[0] for t in triples]
        h = self.H(tag, hs)
        if ordered is None and all(t[0] is t[1] for t in triples):
            o = h
        else:
            src = ordered if ordered is not None else triples
            o = self.H(otag or tag, [t[1] for t in src])
        if all(t[0] is t[2] for t in triples):
            n = h
        else:
            n = self.H(tag, [t[2] for t in triples])
        return h, o, n

    def fp(self, v):
        """(h, o, n) of a value."""
        t = type(v)
        handler = self.dispatch.get(t)
        if handler is self._leaf:
            return self._leaf(v)
        i = id(v)
        hit = self.memo.get(i)
        if hit is not None:
            return hit[1]
        if i in self.stack:
            self.backrefs += 1
            tok = b"^" + str(self.depth - self.stack[i]).encode()
            return tok, tok, tok
        self.nodes += 1
        self.stack[i] = self.depth
        self.depth += 1
        before = self.backrefs
        try:
            if handler is None:
                handler = self._classify(v)
            res = handler(v)
        finally:
            self.depth -= 1
            del self.stack[i]
        if self.backrefs == before:
            self.memo[i] = (v, res)
        return res

    def _classify(self, v):
        if isinstance(v, type):
            return self._class
        if isinstance(v, self.weakref_type):
            return self._weakref
        return self._instance

    def triple(self, tok):
        return tok, tok, tok

    # -- leaves --------------------------------------------------------------------------
    def _leaf(self, v):
        t = type(v)
        if v is None:
            tok = b"N"
        elif t is bool:
            tok = b"T" if v else b"F"
        elif t is int:
            tok = b"I" + str(v).encode() if -10**30 < v < 10**30 else self.blake(b"I" + repr(v).encode(),
                                                                             digest_size=16).digest()
        elif t is float:
            tok = b"R" + repr(v).encode()
        elif t is str:
            raw = v.encode("utf-8", "surrogatepass")
            tok = b"S" + raw if len(raw) < 40 else b"L" + self.blake(raw, digest_size=15).digest()
        elif t is bytes:
            tok = b"Y" + v if len(v) < 40 else b"Z" + self.blake(v, digest_size=15).digest()
        else:
            tok = b"X" + repr(v).encode()
        return tok, tok, tok

    def _reprleaf(self, v):
        return self.triple(b"r" + type(v).__name__.encode() + b":" + repr(v).encode())

    def _module(self, v):
        return self.triple(b"M" + str(v.__name__).encode())

    def _class(self, v):
        self.add_class(v)
        try:
            name = f"{v.__module__}:{v.__qualname__}"
        except Exception:  # noqa: BLE001
            name = "?" + type(v).__name__
        return self.triple(b"C" + name.encode("utf-8", "surrogatepass"))

    def add_class(self, cls):
        if id(cls) in self.seen_classes:
            return
        try:
            mod = cls.__dict__.get("__module__", None) if isinstance(cls.__dict__, dict) or hasattr(
                cls.__dict__, "get") else None
        except Exception:  # noqa: BLE001
            return
        if not _is_sqlglot_name(mod):
            return
        self.seen_classes.add(id(cls))
        self.pending[id(cls)] = cls

    # -- containers ------------------------------------------------------------------------
    def _seq(self, v):
        return self.combine(b"l" if type(v) is list else b"t", [self.fp(x) for x in v])

    def _set(self, v):
        items = sorted((self.fp(x) for x in v), key=lambda t: t[0])
        h = self.H(b"s", [t[0] for t in items])
        o = h if all(t[0] is t[1] for t in items) else self.H(b"s", sorted(t[1] for t in items))
        n = h if all(t[0] is t[2] for t in items) else self.H(b"s", sorted(t[2] for t in items))
        return h, o, n

    def _pairs(self, items):
        pairs = []
        for k, val in items:
            kt, vt = self.fp(k), self.fp(val)
            pairs.append((kt, vt))
        return pairs

    def _dict_from_pairs(self, pairs):
        ph = sorted(self.H(b"p", (k[0], x[0])) for k, x in pairs)
        h = self.H(b"d", ph)
        o = self.H(b"D", [self.H(b"p", (k[1], x[1])) for k, x in pairs])
        if all(k[0] is k[2] and x[0] is x[2] for k, x in pairs):
            n = h
        else:
            n = self.H(b"d", sorted(self.H(b"p", (k[2], x[2])) for k, x in pairs))
        return h, o, n

    def _dict(self, v):
        return self._dict_from_pairs(self._pairs(list(v.items())))

    def _mapping(self, v):
        return self.combine(b"mp", [self._dict_from_pairs(self._pairs(list(v.items())))])

    # -- code and functions ------------------------------------------------------------------
    def _code(self, c):
        consts = [self.fp(x) for x in c.co_consts]
        h = self.H(b"K", [c.co_code, self.H(b"k", [t[0] for t in consts]),
                          repr((c.co_names, c.co_varnames, c.co_freevars, c.co_cellvars, c.co_argcount,
                                c.co_posonlyargcount, c.co_kwonlyargcount, c.co_flags, c.co_nlocals,
                                c.co_stacksize, c.co_name, getattr(c, "co_qualname", None))).encode(),
                          getattr(c, "co_exceptiontable", b"")])
        o = h if all(t[0] is t[1] for t in consts) else self.H(b"K", [h, self.H(b"k", [t[1] for t in consts])])
        return h, o, b"CODE"  # n masks every code object

    def func_parts(self, f):
        """((code, defaults, closure, other) triples, first_line, qualname) of a plain function."""
        code = self.fp(f.__code__)
        defaults = self.combine(b"fd", [self.fp(f.__defaults__), self.fp(f.__kwdefaults__)])
        cells = []
        for cell in f.__closure__ or ():
            try:
                cells.append(self.fp(cell.cell_contents))
            except ValueError:
                cells.append(self.triple(b"empty"))
        closure = self.combine(b"fc", cells)
        if "__annotate__" in type(f).__dict__:
            ann = self.fp(f.__annotate__)  # 3.14: lazily evaluated; never evaluate here
        else:
            ann = self.fp(f.__annotations__)
        other = self.combine(b"fo", [self.fp(f.__qualname__), self.fp(f.__name__), self.fp(f.__module__),
                                     self.fp(f.__doc__), self.fp(f.__dict__), ann,
                                     self.fp(getattr(f, "__type_params__", ()))])
        return (code, defaults, closure, other), f.__code__.co_firstlineno, f.__qualname__

    def _function(self, f):
        parts, _, _ = self.func_parts(f)
        return self.combine(b"F", list(parts))

    def _cellobj(self, c):
        try:
            return self.combine(b"cell", [self.fp(c.cell_contents)])
        except ValueError:
            return self.triple(b"cell-empty")

    def _method(self, m):
        return self.combine(b"m", [self.fp(m.__func__), self.fp(m.__self__)])

    def _wrapper(self, w):
        d = {k: x for k, x in vars(w).items() if k != "__wrapped__"} if hasattr(w, "__dict__") else {}
        return self.combine(b"w" + type(w).__name__.encode(), [self.fp(w.__func__), self.fp(d)])

    def _property(self, p):
        return self.combine(b"P", [self.fp(p.fget), self.fp(p.fset), self.fp(p.fdel), self.fp(p.__doc__)])

    def _partial(self, p):
        d = vars(p) if hasattr(p, "__dict__") else {}
        return self.combine(b"pa", [self.fp(p.func), self.fp(p.args), self.fp(p.keywords), self.fp(d)])

    def _lru(self, w):
        info = w.cache_info()
        if info.currsize:
            self.warm.append(f"{_cell_repr(self.cell)}: {_name_of(w)} currsize={info.currsize}")
        d = {k: x for k, x in vars(w).items() if k != "__wrapped__"}
        return self.combine(b"lru", [self.fp(getattr(w, "__wrapped__", None)), self.fp(tuple(info)), self.fp(d)])

    def _builtin(self, b):
        s = getattr(b, "__self__", None)
        name = (str(getattr(b, "__module__", None)) + "." + str(getattr(b, "__qualname__", b.__name__))).encode()
        if s is None or isinstance(s, self.types.ModuleType):
            return self.triple(b"B" + name)
        return self.combine(b"b", [self.triple(b"B" + name), self.fp(s)])

    def _descriptor(self, d):
        owner = getattr(d, "__objclass__", None)
        s = getattr(d, "__self__", None)
        name = str(getattr(d, "__name__", "?"))
        base = self.triple(b"D" + type(d).__name__.encode() + b":" + _name_of(owner).encode() + b"." + name.encode())
        if s is not None and not isinstance(s, (type, self.types.ModuleType)):
            return self.combine(b"Db", [base, self.fp(s)])  # bound method-wrapper
        return base

    def _lock(self, lk):
        """A lock's only state is whether it is held (and, for an RLock, owner/count): its repr
        without the address. After a clean import every lock should be free."""
        return self.triple(b"lock:" + self.addr.sub("?", repr(lk)).encode())

    def _pattern(self, p):
        return self.triple(b"re" + repr((p.pattern, p.flags)).encode())

    def _weakref(self, r):
        return self.combine(b"wr", [self.fp(r())])

    # -- instances ----------------------------------------------------------------------------
    def _instance(self, v):
        t = type(v)
        parts = [self._class(t)]
        handled = False
        try:
            d = object.__getattribute__(v, "__dict__")
        except Exception:  # noqa: BLE001
            d = None
        if isinstance(d, dict):
            parts.append(self._dict(d))
            handled = True
        for klass in t.__mro__:
            slots = klass.__dict__.get("__slots__")
            if slots is None:
                continue
            if isinstance(slots, str):
                slots = (slots,)
            for s in slots:
                if s in ("__dict__", "__weakref__"):
                    continue
                desc = klass.__dict__.get(s if not (s.startswith("__") and not s.endswith("__"))
                                          else f"_{klass.__name__.lstrip('_')}{s}")
                if desc is None or not hasattr(desc, "__get__"):
                    continue
                try:
                    parts.append(self.combine(b"sl", [self.fp(s), self.fp(desc.__get__(v, t))]))
                except AttributeError:
                    parts.append(self.combine(b"sl", [self.fp(s), self.triple(b"unset")]))
                handled = True
        payload = self._payload(v)
        if payload is not None:
            parts.append(payload)
            handled = True
        if not handled:
            r = None
            try:
                r = repr(v)
            except Exception:  # noqa: BLE001
                pass
            if r is not None and not self.addr.search(r) and len(r) < 4096:
                self.repr_types.append(_name_of(t))
                parts.append(self.triple(b"repr:" + r.encode("utf-8", "surrogatepass")))
            else:
                self.opaque.append(f"{_cell_repr(self.cell)}: {_name_of(t)}")
                parts.append(self.triple(b"opaque"))
        return self.combine(b"O", parts)

    def _payload(self, v):
        """Content of a builtin base type (dict/list/set/... subclasses), read with the base's own methods."""
        if isinstance(v, dict):
            res = self._dict_from_pairs(self._pairs(list(dict.items(v))))
        elif isinstance(v, (list, tuple)):
            base = list if isinstance(v, list) else tuple
            res = self.combine(b"l", [self.fp(x) for x in base.__iter__(v)])
        elif isinstance(v, (set, frozenset)):
            base = set if isinstance(v, set) else frozenset
            res = self._set(list(base.__iter__(v)))
        elif isinstance(v, str):
            res = self._leaf(str.__str__(v))
        elif isinstance(v, bool):
            res = self._leaf(bool(v))
        elif isinstance(v, int):
            res = self._leaf(int.__int__(v))
        elif isinstance(v, float):
            res = self._leaf(float.__float__(v))
        elif isinstance(v, bytes):
            res = self._leaf(bytes(v))
        else:
            dq = sys.modules.get("collections")
            if dq is not None and isinstance(v, dq.deque):
                return self.combine(b"dq", [self.fp(x) for x in list(v)] + [self.fp(v.maxlen)])
            return None
        if isinstance(v, dict) and dq_default(v) is not None:
            res = self.combine(b"dd", [res, self.fp(dq_default(v))])
        return res

    # -- cells --------------------------------------------------------------------------------
    def record(self, v):
        """Cell record: (h, o, n, kind, items, func_parts)."""
        if isinstance(v, type):
            self.anchors.setdefault(id(v), []).append(_cell_repr(self.cell))
        h, o, n = self.fp(v)
        kind, items, fparts = "other", None, None
        if isinstance(v, dict):
            kind = "dict"
            items = [self._item(k, x) for k, x in list(dict.items(v))]
        elif isinstance(v, (set, frozenset)):
            kind = "set"
            items = sorted((self._item(x, x) for x in list(v)), key=repr)
        elif isinstance(v, (list, tuple)):
            kind = "seq"
            items = [self._item(x, x) for x in list(v)]
        else:
            fn = self._underlying(v)
            if fn is not None:
                kind = "func"
                fparts = self._fparts(v, fn)
        return (h, o, n, kind, items, fparts)

    def _item(self, k, x):
        kid = id(k)
        hit = self.kmemo.get(kid)
        if hit is not None and hit[0] is k:
            kf = hit[1]
        else:
            kf = self.key_fp(k)
            if type(k) not in (str, int):
                self.kmemo[kid] = (k, kf)
        vh, _, vn = self.fp(x)
        return (kf, self.short(vh), b"" if vn is vh else self.short(vn))

    def short(self, tok):
        return tok if len(tok) <= 8 else self.blake(tok, digest_size=8).digest()

    def _underlying(self, v):
        t = type(v)
        ty = self.types
        if t is ty.FunctionType:
            return v
        if t in (staticmethod, classmethod, ty.MethodType):
            return self._underlying(v.__func__)
        if t is property:
            return v.fget if type(v.fget) is ty.FunctionType else None
        if t is self.partial_type:
            return self._underlying(v.func)
        if t is self.lru_type:
            return self._underlying(getattr(v, "__wrapped__", None))
        return None

    def _fparts(self, v, fn):
        """Component hashes (code, defaults, closure, other) of a function-like value. For
        wrappers, the wrapper's own state goes into `other`; a property's fset/fdel too."""
        (code, defaults, closure, other), first, qual = self.func_parts(fn)
        if v is not fn:
            # Everything of v except fn's own code/defaults/closure, so "code" stays code-only.
            other = self.combine(b"fw", [other, self._wrapper_rest(v, fn)])
        return (code[0], defaults[0], closure[0], other[0], first, qual)

    def _wrapper_rest(self, v, fn):
        t = type(v)
        if t is property:
            return self.combine(b"Pr", [self.fp(v.fset), self.fp(v.fdel), self.fp(v.__doc__)])
        if t in (staticmethod, classmethod):
            d = {k: x for k, x in vars(v).items() if k != "__wrapped__"} if hasattr(v, "__dict__") else {}
            inner = self._wrapper_rest(v.__func__, fn) if v.__func__ is not fn else self.triple(b"-")
            return self.combine(b"wr" + t.__name__.encode(), [self.fp(d), inner])
        if t is self.types.MethodType:
            inner = self._wrapper_rest(v.__func__, fn) if v.__func__ is not fn else self.triple(b"-")
            return self.combine(b"mr", [self.fp(v.__self__), inner])
        if t is self.partial_type:
            inner = self._wrapper_rest(v.func, fn) if v.func is not fn else self.triple(b"-")
            return self.combine(b"par", [self.fp(v.args), self.fp(v.keywords), inner])
        if t is self.lru_type:
            d = {k: x for k, x in vars(v).items() if k != "__wrapped__"}
            inner = self._wrapper_rest(v.__wrapped__, fn) if v.__wrapped__ is not fn else self.triple(b"-")
            return self.combine(b"lrur", [self.fp(tuple(v.cache_info())), self.fp(d), inner])
        return self.fp(v)


def dq_default(v):
    dd = sys.modules.get("collections")
    if dd is not None and isinstance(v, dd.defaultdict):
        return v.default_factory
    return None


def _cell_repr(cell):
    if cell is None:
        return "?"
    return f"{cell[0]}:{cell[1]}:{cell[2]}"


if __name__ == "__main__":
    try:
        _child(sys.argv[1])
    except BaseException:  # noqa: BLE001
        pass
    os._exit(1)
