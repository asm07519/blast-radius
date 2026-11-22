"""Per-process dependency tracer used only when building maps (never at run time).

Records, for the process it runs in:
  * call lines   - (file, line) executed inside a function, lambda or generator
                   expression code object (CO_OPTIMIZED set)
  * import lines - (file, line) executed in module or class-body code
  * reads        - repository files opened as data (not by the import system)
  * listed       - repository directories listed (not by the import system)

Lines are recorded with sys.monitoring and every location is disabled after its
first hit, so tracing costs almost nothing once a line has been seen. The data
is written as JSON to $BR_TRACE_DIR/<pid>.json when the process exits, including
spawned multiprocessing children (they import sitecustomize too) and forked
children that leave through os._exit.
"""

import atexit
import json
import os
import sys

_REPO = os.environ.get("BR_REPO", "").rstrip("/") + "/"
_OUT = os.environ.get("BR_TRACE_DIR", "")
_TOOL = sys.monitoring.COVERAGE_ID
_DISABLE = sys.monitoring.DISABLE
_CO_OPTIMIZED = 0x1

_call = set()
_imp = set()
_call_at_import = set()  # call-time lines that ran while some module was being executed
_reads = {}
_listed = set()
_classes = set()  # "module:qualname" of every class in the MRO of a recorded receiver
_classes_at_import = set()
_seen_types = {}
_started = False
_written = False
_extra = {}
_import_depth = 0
_SQLGLOT = None
_RECV_TOOL = 3
_IMPORT_TOOL = 4
_HOOKS = {"__init_subclass__", "__set_name__", "__class_getitem__", "__prepare__", "__instancecheck__",
          "__subclasscheck__"}
_ENTRY = {"__init__", "__new__", "__post_init__", "__setstate__", "__copy__", "__deepcopy__", "__reduce__",
          "__reduce_ex__"}


def _on_line(code, line):
    fn = code.co_filename
    if fn.startswith(_REPO):
        if code.co_flags & _CO_OPTIMIZED:
            _call.add((fn, line))
        else:
            _imp.add((fn, line))
    return _DISABLE


def _on_import_line(code, line):
    if code.co_flags & _CO_OPTIMIZED and code.co_filename.startswith(_REPO):
        _call_at_import.add((code.co_filename, line))
    return _DISABLE


def _class_keys(tp):
    keys = _seen_types.get(tp)
    if keys is None:
        keys = []
        for k in getattr(tp, "__mro__", ()):
            if k is object:
                continue
            keys.append(f"{getattr(k, '__module__', '?')}:{getattr(k, '__qualname__', getattr(k, '__name__', '?'))}")
        keys = tuple(keys)
        _seen_types[tp] = keys
    return keys


def _on_start(code, offset):
    """Record the class of self/cls when an object or class enters use."""
    if not code.co_filename.startswith(_SQLGLOT) or code.co_argcount < 1:
        return _DISABLE
    first = code.co_varnames[0]
    name = code.co_name
    if first == "cls":
        if name in _HOOKS:
            return _DISABLE
    elif first == "self":
        if name not in _ENTRY:
            return _DISABLE
    else:
        return _DISABLE
    try:
        obj = sys._getframe(1).f_locals.get(first)
    except Exception:
        return None
    if obj is None:
        return None
    tp = obj if (first == "cls" and isinstance(obj, type)) else type(obj)
    if tp in _seen_types and not _import_depth:
        return None
    keys = _class_keys(tp)
    _classes.update(keys)
    if _import_depth:
        _classes_at_import.update(keys)
    return None


def _patch_imports():
    """Track when module code is executing (an import in progress)."""
    import importlib._bootstrap_external as be

    original = be._LoaderBasics.exec_module

    def exec_module(self, module):
        global _import_depth
        if _import_depth == 0:
            sys.monitoring.set_events(_IMPORT_TOOL, sys.monitoring.events.LINE)
        _import_depth += 1
        try:
            return original(self, module)
        finally:
            _import_depth -= 1
            if _import_depth == 0:
                sys.monitoring.set_events(_IMPORT_TOOL, 0)

    be._LoaderBasics.exec_module = exec_module


def _caller_kind():
    """Classify who opened a file by walking a few frames up the stack."""
    try:
        f = sys._getframe(2)
    except ValueError:
        return "data"
    depth = 0
    via = "data"
    while f is not None and depth < 12:
        fn = f.f_code.co_filename
        if "importlib" in fn or fn.startswith("<frozen"):
            return "import"
        if "_pytest/assertion" in fn or "_pytest/pathlib" in fn or "_pytest/python.py" in fn:
            return "import"
        if fn.endswith("linecache.py") or fn.endswith("traceback.py") or fn.endswith("warnings.py"):
            via = "linecache"
        f = f.f_back
        depth += 1
    return via


def _audit(event, args):
    if event == "open":
        path = args[0]
        if path is None or isinstance(path, int):
            return
        try:
            p = os.fsdecode(path)
        except Exception:
            return
        if not p.startswith("/"):
            try:
                p = os.path.abspath(p)
            except Exception:
                return
        if not p.startswith(_REPO) or p.endswith(".pyc") or "/__pycache__/" in p:
            return
        kind = _caller_kind()
        if kind == "import":
            return
        rel = p[len(_REPO):]
        if _reads.get(rel) != "data":
            _reads[rel] = kind
    elif event in ("os.listdir", "os.scandir", "glob.glob", "os.walk"):
        path = args[0] if args else None
        if path is None:
            path = "."
        if isinstance(path, int):
            return
        try:
            p = os.fsdecode(path)
        except Exception:
            return
        if not p.startswith("/"):
            p = os.path.abspath(p)
        if not p.startswith(_REPO.rstrip("/")):
            return
        if _caller_kind() == "import":
            return
        _listed.add(p[len(_REPO):] if len(p) >= len(_REPO) else "")


def _write():
    global _written
    if _written or not _OUT:
        return
    _written = True
    for tool in (_TOOL, _RECV_TOOL, _IMPORT_TOOL):
        try:
            sys.monitoring.set_events(tool, 0)
        except Exception:
            pass
    n = len(_REPO)
    data = {
        "pid": os.getpid(),
        "argv": sys.argv[:3],
        "call": sorted([f[n:], l] for f, l in _call),
        "import": sorted([f[n:], l] for f, l in _imp),
        "call_at_import": sorted([f[n:], l] for f, l in _call_at_import),
        "classes": sorted(_classes),
        "classes_at_import": sorted(_classes_at_import),
        "reads": _reads,
        "listed": sorted(_listed),
        "extra": _extra,
    }
    if os.environ.get("BR_CELLS") == "1":
        try:
            import br_cells

            data["cells"] = br_cells.dump()
        except Exception as error:
            data["cells"] = {"alarms": [f"dump failed: {error!r}"], "proxies": [], "unproxied": [],
                             "collisions": [], "dyn": {"test": [], "import": []}}
    tmp = os.path.join(_OUT, f".{os.getpid()}.tmp")
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, os.path.join(_OUT, f"{os.getpid()}.json"))


def note(key, value):
    """Lets the pytest plugin or the duckdb stub attach facts to this process."""
    _extra.setdefault(key, []).append(value)


def start():
    global _started
    if _started or not _OUT or _REPO == "/":
        return
    _started = True
    global _SQLGLOT
    _SQLGLOT = _REPO + "sqlglot/"
    sys.monitoring.use_tool_id(_TOOL, "blast-radius")
    sys.monitoring.register_callback(_TOOL, sys.monitoring.events.LINE, _on_line)
    sys.monitoring.set_events(_TOOL, sys.monitoring.events.LINE)
    sys.monitoring.use_tool_id(_RECV_TOOL, "blast-radius-receivers")
    sys.monitoring.register_callback(_RECV_TOOL, sys.monitoring.events.PY_START, _on_start)
    sys.monitoring.set_events(_RECV_TOOL, sys.monitoring.events.PY_START)
    sys.monitoring.use_tool_id(_IMPORT_TOOL, "blast-radius-imports")
    sys.monitoring.register_callback(_IMPORT_TOOL, sys.monitoring.events.LINE, _on_import_line)
    _patch_imports()
    if os.environ.get("BR_CELLS") == "1":
        import br_cells

        br_cells.install()
    sys.addaudithook(_audit)
    atexit.register(_write)

    real_exit = os._exit

    def _exit(code):
        try:
            _write()
        finally:
            real_exit(code)

    os._exit = _exit

    def _after_fork():
        global _written
        _written = False

    os.register_at_fork(after_in_child=_after_fork)
