"""Does the patched sqlglot still import? One fresh interpreter, every patched module overlaid.

A literal swap in a class body can make the import fail even though the literal itself
builds fine: a class-creation hook consumes it (`arg_types = ["this"]` breaks
Expr.__init_subclass__, `HEREDOC_STRINGS = [("$",)]` breaks the tokenizer's). Then every
importer fails, not only the class's users. BR_LITERAL_PROBE makes every literal narrowing
conditional on this probe (selector/impact.py).

Parent side (imported by the selector):
    run(patched_files, src_root, modules) -> bool
    modules_from_maps(maps) -> list[str]
Child side (this file as a script, never imported by it):
    python -I -S import_probe.py <spec.json>

The child installs an in-memory finder for the patched modules (the technique of
replica_runner.py, duplicated here on purpose), chdirs to the source root, imports `sqlglot`
and then every listed module, in order. Any exception, SystemExit included, is a failure.

Success marker: the spec names a result file and a random nonce; the child deletes the spec
before importing anything and, only after the last import, creates the result file
exclusively and writes "OK <nonce>". The parent requires exit status 0 and exactly that
content. Code run at import cannot produce it by printing (stdout and stderr go to
/dev/null and are never parsed), by exiting early (sys.exit is caught, os._exit leaves no
file), or by guessing the nonce (it never sees the spec); only code written to defeat the
probe on purpose could, which is outside what this guards against.

What the probe does not model: pytest's deeper stack (the child lowers the recursion limit
by 250 frames to stay conservative), test modules' own imports (their import-time reads of
sqlglot class attributes are handled statically by impact.refine_class_literal), import
orders other than `sqlglot` first and then sorted module names (every test imports the
`sqlglot` package first, because importing any submodule runs sqlglot/__init__.py first),
and site-packages (-S: mypy_extensions is absent, so sqlglot.helper uses its own identity
`trait`/`mypyc_attr`, which is what the installed ones are at run time as well).
Warnings are not failures: the pinned repository has no pytest configuration (no
[tool.pytest] in pyproject.toml, no pytest.ini, tox.ini or setup.cfg section, no
filterwarnings in tests/), so a new DeprecationWarning or SyntaxWarning does not fail a test.
"""

import base64
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RESULT_LIMIT = 4096


def module_name(path):
    """sqlglot/dialects/duckdb.py -> sqlglot.dialects.duckdb; a package's __init__.py -> the package."""
    name = path[:-3].replace("/", ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


def modules_from_maps(maps):
    """Every sqlglot module at least one test imported at baseline, as sorted dotted names.

    Dialect modules that tests load lazily (Dialect.get_or_raise) are included: their module
    code ran in those tests' processes, so the maps record them as imported. __main__ modules
    are left out (importing one runs a program). sqlglotc/ is not a package (it holds only the
    mypyc build's setup.py, run by tests/test_sqlglotc.py through runpy), so it is not listed.
    """
    out = set()
    for f in maps.files:
        if not (f.startswith("sqlglot/") and f.endswith(".py")) or not maps.imported(f):
            continue
        name = module_name(f)
        parts = name.split(".")
        if parts[-1] == "__main__" or not all(p.isidentifier() for p in parts):
            continue
        out.add(name)
    return sorted(out)


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the child leads its own session: take its children too
    except (OSError, AttributeError):
        try:
            proc.kill()
        except OSError:
            pass


def run_detailed(patched_files, src_root, modules, timeout=10.0, python=sys.executable):
    """(ok, detail). ok is True only when the child positively reported a clean import."""
    tmp = None
    proc = None
    try:
        tmp = tempfile.mkdtemp(prefix="br-probe-")
        spec_path = os.path.join(tmp, "spec.json")
        result_path = os.path.join(tmp, "result")
        nonce = secrets.token_hex(16)
        spec = {
            "src": os.path.abspath(src_root),
            "patched": {module_name(p): [os.path.join(os.path.abspath(src_root), p), base64.b64encode(src).decode()]
                        for p, src in patched_files.items()},
            "modules": list(modules),
            "nonce": nonce,
            "result": result_path,
        }
        with open(spec_path, "w") as fh:
            json.dump(spec, fh)
        proc = subprocess.Popen([python, "-I", "-S", os.path.join(HERE, "import_probe.py"), spec_path],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                cwd=tmp, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            proc.wait()
            return False, f"timeout after {timeout}s"
        try:
            with open(result_path, "rb") as fh:
                got = fh.read(RESULT_LIMIT)
        except OSError:
            return False, f"exit {code}, no result"
        if code == 0 and got == f"OK {nonce}\n".encode():
            return True, "clean import"
        return False, f"exit {code}: {got[:300].decode(errors='replace').strip()}"
    except Exception as error:  # noqa: BLE001 - a probe that cannot run proves nothing
        return False, f"probe error {error!r}"
    finally:
        if proc is not None:
            if proc.poll() is None:
                _kill(proc)
                proc.wait()
            else:
                _kill(proc)  # anything it left behind in its session
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def run(patched_files, src_root, modules, timeout=10.0, python=sys.executable):
    """True only if a fresh interpreter imported `sqlglot` and every module in `modules`
    with `patched_files` ({repo path: new source bytes}) overlaid, without raising."""
    return run_detailed(patched_files, src_root, modules, timeout=timeout, python=python)[0]


# --- child ---------------------------------------------------------------------------------

def _child(spec_path):
    import importlib
    import importlib.abc
    import importlib.machinery
    import importlib.util

    sys.dont_write_bytecode = True  # never write into the shared source tree
    with open(spec_path) as fh:
        spec = json.load(fh)
    os.remove(spec_path)  # code run at import must not find the nonce
    nonce, result = spec.pop("nonce"), spec["result"]

    def report(text):
        fd = os.open(result, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, text.encode(errors="replace")[:RESULT_LIMIT])
        finally:
            os.close(fd)

    try:
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
        # Tests import from deeper stacks (pytest frames): leave less room, never more.
        sys.setrecursionlimit(max(200, sys.getrecursionlimit() - 250))
        importlib.import_module("sqlglot")
        for name in spec["modules"]:
            importlib.import_module(name)
        for name in patched:
            mod = sys.modules.get(name)
            if mod is not None and not isinstance(getattr(getattr(mod, "__spec__", None), "loader", None), Loader):
                raise ImportError(f"{name} was not loaded from the overlay")
    except BaseException as error:  # noqa: BLE001 - SystemExit and KeyboardInterrupt included
        try:
            report(f"FAIL {type(error).__name__}: {error!r}"[:300])
        except BaseException:  # noqa: BLE001
            pass
        os._exit(1)
    report(f"OK {nonce}\n")
    os._exit(0)  # skip interpreter teardown: atexit hooks and threads of the imported code


if __name__ == "__main__":
    try:
        _child(sys.argv[1])
    except BaseException:  # noqa: BLE001
        pass
    os._exit(1)
