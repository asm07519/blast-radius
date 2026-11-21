"""From a parsed patch to the set of tests that could fail.

Everything here is an over-approximation: a test is left out only when the maps
show it cannot reach the changed code or data. Anything not understood selects
every test, which costs ~37 s instead of a 300 s miss.
"""

import configparser
import os
import posixpath

from . import pyast
from .patch import PatchError, apply

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

PYTEST_CONFIGS = {"pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini"}
ALWAYS_ALL = {"conftest.py", "sitecustomize.py", "usercustomize.py"}
WATCHED = ("sqlglot/", "tests/", "sqlglotc/")


class Everything(Exception):
    """Raised when a change can affect any test."""


class Result:
    def __init__(self):
        self.bits = 0
        self.nontrivial = False
        self.notes = []

    def add(self, bits, note=None):
        self.bits |= bits
        if note:
            self.notes.append(note)


def read_src(src_root, path):
    full = os.path.join(src_root, path)
    try:
        with open(full, "rb") as fh:
            return fh.read()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def _watched(path):
    """Paths whose changes can matter to tests whose traces are incomplete."""
    return path.startswith(WATCHED) or ("/" not in path and path.endswith(".py"))


def commit_impact(file_patches, src_root, maps, import_refiner=None):
    res = Result()
    for fp in file_patches:
        if fp.mode_only:
            continue
        old_path, new_path = fp.old_path, fp.new_path
        copy = getattr(fp, "copy", False)
        if old_path and new_path and old_path != new_path and not copy:
            # rename: the old path disappears and the new one appears
            _deleted(old_path, src_root, maps, res)
            _added(new_path, src_root, maps, res, fp)
            continue
        if copy:
            _added(new_path, src_root, maps, res, fp)
            continue
        if old_path is None:
            _added(new_path, src_root, maps, res, fp)
        elif new_path is None:
            _deleted(old_path, src_root, maps, res)
        else:
            _modified(fp, old_path, src_root, maps, res, import_refiner)
    if res.nontrivial:
        res.add(maps.untrusted)
    return res


def _base(path):
    return posixpath.basename(path)


def _special(path):
    base = _base(path)
    if base in ALWAYS_ALL:
        raise Everything(f"{path}: interpreter/pytest hook")
    if "/" not in path and path.endswith(".py") and path != "setup.py":
        raise Everything(f"{path}: top-level module can shadow imports")


def _added(path, src_root, maps, res, fp):
    _special(path)
    if path in PYTEST_CONFIGS:
        old = read_src(src_root, path)
        if old is None:
            raise Everything(f"{path}: new pytest config")
        return _config(path, old, _apply_or_none(fp, old), res)
    if read_src(src_root, path) is not None:
        # "new file" over an existing path: treat as a modification
        raise Everything(f"{path}: added over existing file")
    directory = posixpath.dirname(path)
    res.add(maps.listers(directory), f"{path}: added, directory listers")
    if path.endswith("/__init__.py"):
        module = directory + ".py"
        if read_src(src_root, module) is not None:
            res.add(maps.imported(module) | maps.reads(module), f"{path}: package shadows {module}")
    if path.endswith(".py") and _watched(path):
        res.nontrivial = True
    elif _watched(path):
        res.nontrivial = True


def _deleted(path, src_root, maps, res):
    _special(path)
    if path in PYTEST_CONFIGS:
        raise Everything(f"{path}: pytest config removed")
    directory = posixpath.dirname(path)
    bits = maps.reads(path) | maps.listers(directory)
    if path.endswith(".py"):
        bits |= maps.imported(path)
    res.add(bits, f"{path}: deleted")
    if _watched(path):
        res.nontrivial = True


def _apply_or_none(fp, old):
    try:
        return apply(fp, old)
    except PatchError:
        return None


def _config(path, old, new, res):
    if new is None:
        raise Everything(f"{path}: cannot apply")
    if old == new:
        return
    try:
        if path == "pyproject.toml":
            if tomllib is None:
                raise Everything("no tomllib")
            a = tomllib.loads(old.decode("utf-8"))
            b = tomllib.loads(new.decode("utf-8"))
            if a.get("tool", {}).get("pytest") != b.get("tool", {}).get("pytest"):
                raise Everything(f"{path}: pytest settings changed")
            res.notes.append(f"{path}: no pytest settings changed")
            return
        if path == "setup.cfg":
            pa, pb = configparser.RawConfigParser(), configparser.RawConfigParser()
            pa.read_string(old.decode("utf-8"))
            pb.read_string(new.decode("utf-8"))
            for section in ("tool:pytest", "pytest"):
                sa = dict(pa.items(section)) if pa.has_section(section) else None
                sb = dict(pb.items(section)) if pb.has_section(section) else None
                if sa != sb:
                    raise Everything(f"{path}: pytest settings changed")
            res.notes.append(f"{path}: no pytest settings changed")
            return
    except Everything:
        raise
    except Exception as error:
        raise Everything(f"{path}: config does not parse ({error!r})")
    raise Everything(f"{path}: pytest config changed")


def _modified(fp, path, src_root, maps, res, import_refiner):
    _special(path)
    old = read_src(src_root, path)
    if old is None:
        return _added(path, src_root, maps, res, fp)
    if path in PYTEST_CONFIGS:
        return _config(path, old, _apply_or_none(fp, old), res)
    if not path.endswith(".py"):
        res.add(maps.reads(path), f"{path}: data readers")
        if _watched(path):
            res.nontrivial = True
        return
    if fp.binary:
        new = None
    else:
        new = _apply_or_none(fp, old)
    if new is not None and new == old:
        return
    # Anything reading the file's text sees any edit, comments included.
    res.add(maps.reads(path), None)
    if new is None:
        res.add(maps.imported(path), f"{path}: patch not applicable, all importers")
        res.nontrivial = True
        return
    sites = pyast.analyse(old, new, key=path)
    for site in sites:
        if site.kind == "call":
            res.add(maps.call(path, site.lines), f"{path}: {site}")
        elif site.kind == "import":
            bits = None
            if import_refiner is not None:
                bits = import_refiner(path, old, new, site, maps)
            if bits is None:
                bits = maps.imported(path)
            res.add(bits, f"{path}: {site}")
        elif site.kind == "doc":
            res.add(maps.reads(path), f"{path}: {site}")
        else:  # syntax or unknown
            res.add(maps.imported(path) | maps.reads(path), f"{path}: {site}")
    if sites:
        res.nontrivial = True
    else:
        res.notes.append(f"{path}: no semantic change")


# --- narrowing class-body literal changes -------------------------------------------------

def _module_of(path):
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    return module


_CLASS_HOOKS = {"__new__", "__init__", "__init_subclass__", "__set_name__", "__prepare__"}
_CLASSY = ("cls", "class", "klass", "kls", "_type", "typ", "tp", "meta", "mcs", "subclass", "expr_type")


def _names_an_instance(base):
    """A plain variable (or self.attr) whose name does not suggest it holds a class object."""
    parts = base.split(".")
    if not all(p.isidentifier() for p in parts):
        return False
    if parts[0] == "self":
        parts = parts[1:]
    if not parts or len(parts) > 1:
        return False
    name = parts[0]
    if not name.islower() or name in ("type",):
        return False
    return not any(name.endswith(c) or name.startswith(c) for c in _CLASSY)


def refine_class_literal(path, old, new, site, maps):
    """Tests that can observe a literal change to a class attribute, or None to fall back.

    A class-body assignment that only swaps literal values cannot fail at import, and
    its new value is seen only through the class or its subclasses. Tests never touch
    those classes unless the tracer recorded an object or classmethod of one of them
    (receivers are recorded with their whole MRO) or the test ran code naming them.
    Falls back (None) whenever the value may also feed state built at import time.
    """
    if not maps.has_class_data or site.kind != "import" or not site.literal or not site.class_path:
        return None
    names = site.names
    if not names or "*" in names or any(n.startswith("__") and n.endswith("__") for n in names):
        return None
    module = _module_of(path)
    path_keys = [f"{module}:{'.'.join(site.class_path[:i])}" for i in range(1, len(site.class_path) + 1)]
    key = path_keys[-1]
    info = maps.class_info.get(key)
    if info is None or not info[0]:
        return None  # objects of this class are invisible to the tracer
    if any(k in maps.classes_at_import for k in path_keys):
        return None  # used while modules were importing: may feed import-time state
    family_simple = set(site.class_path)
    family_top = site.class_path[0]

    def other_class(base):
        """True when `base` plainly names some other class (e.g. exp.Select, tokens.Tokenizer)."""
        parts = base.split(".")
        last = parts[-1]
        if parts[0] in ("self", "cls", "klass", "mcs", "metacls"):
            return False
        if parts[0] == family_top or last in family_simple:
            return False
        return last[:1].isupper() and not last.isupper()

    use_readers = True
    for n in names:
        rec = maps.names.get(n)
        if rec is None:
            continue
        _, import_loads, call_import_loads = rec
        for enclosing, base in import_loads:
            if enclosing:
                mro = maps.class_info.get(enclosing, (False, ()))[1]
                if key in mro:
                    use_readers = False  # a subclass derives another attribute from it
                    continue
            if base == "":
                continue  # a variable of that name, not the attribute
            if base in ('"', "?") or not other_class(base):
                return None
        for base, owner in call_import_loads:
            if base == "":
                continue
            if base in ("self", "cls"):
                use_readers = False  # read through a receiver; none of this family existed at import
                continue
            if base in ('"', "?"):
                return None
            if other_class(base):
                continue
            if owner in _CLASS_HOOKS:
                use_readers = False  # derived while creating a class: stored on that class
                continue
            if _names_an_instance(base):
                # An object, and no object of this family existed while importing.
                use_readers = False
                continue
            return None

    users = 0
    for k in path_keys:
        users |= maps.class_users(k)
    # Code that names the class (or a subclass) can read the attribute without an object.
    refs = {family_top}
    for ckey, (_, mro) in maps.class_info.items():
        if any(k in mro for k in path_keys):
            refs.add(ckey.split(":", 1)[1].split(".", 1)[0])
    for r in refs:
        users |= maps.name_readers(r)
    bits = maps.imported(path) & users
    if use_readers:
        readers = 0
        for n in names:
            readers |= maps.name_readers(n) | maps.dynamic_readers(n, family_key=key)
        bits &= readers
    return bits
