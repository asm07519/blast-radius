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
        # For subtest replicas: every change is a call-time edit to a sqlglot module.
        self.replica_ok = True
        self.call_sites = []  # (path, lines)
        self.import_files = set()  # sqlglot modules changed at import time
        self.patched = {}  # path -> new source bytes
        # BR_LITERAL_PROBE: literal narrowings that hold only if the patched code imports
        # cleanly, as (narrow bits, fallback bits, note); resolved once per commit.
        self.conditional = []
        # Why an import probe of res.patched would not be faithful to the patched tree.
        self.probe_blockers = []
        self.probe_calls = 0

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


def commit_impact(file_patches, src_root, maps, import_refiner=None, import_probe=None):
    """Tests a patch can affect, as a Result.

    import_probe(patched: dict[path, bytes]) -> bool is used only with BR_LITERAL_PROBE=1: it
    must return True only after it saw a fresh interpreter import `sqlglot` and every sqlglot
    module some test imports, with all of `patched` overlaid (selector/import_probe.py). It is
    called at most once per commit, after every file has been analysed (PRD §6 rule 4).
    """
    res = Result()
    seen = set()
    for fp in file_patches:
        if fp.mode_only:
            continue
        old_path, new_path = fp.old_path, fp.new_path
        for p in {old_path, new_path} - {None}:
            if p in seen:
                res.probe_blockers.append(f"{p}: patched twice")
            seen.add(p)
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
    if res.conditional:
        _resolve_conditional(res, import_probe)
    return res


def _resolve_conditional(res, import_probe):
    """Apply the literal narrowings of the whole commit only if the patched code imports cleanly.

    One probe covers every narrowing of the commit, because it imports all patched modules
    together. No probe, a probe that raises or says anything but True, or a patch the
    in-memory overlay cannot represent: every narrowing falls back to all importers.
    """
    narrow = fallback = 0
    for n, f, _ in res.conditional:
        narrow |= n
        fallback |= f
    ok, why = False, None
    if not fallback & ~(res.bits | narrow):
        why = "falling back adds no test"
    elif res.probe_blockers:
        why = f"no import probe ({res.probe_blockers[0]})"
    elif import_probe is None:
        why = "no import probe available"
    else:
        res.probe_calls += 1
        try:
            ok = import_probe(dict(res.patched)) is True
        except Exception as error:  # noqa: BLE001 - an unusable probe proves nothing
            why = f"import probe failed ({error!r})"
        if not ok and why is None:
            why = "patched code did not import cleanly"
    for n, f, note in res.conditional:
        res.add(n if ok else f, note if ok else f"{note}; all importers")
    res.notes.append(f"literal narrowing kept: patched code imports cleanly ({len(res.conditional)} site(s))"
                     if ok else f"literal narrowing dropped: {why}")


def _base(path):
    return posixpath.basename(path)


def _special(path):
    base = _base(path)
    if base in ALWAYS_ALL:
        raise Everything(f"{path}: interpreter/pytest hook")
    if "/" not in path and path.endswith(".py") and path != "setup.py":
        raise Everything(f"{path}: top-level module can shadow imports")


def _added(path, src_root, maps, res, fp):
    res.replica_ok = False
    res.probe_blockers.append(f"{path}: added")
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
    res.replica_ok = False
    res.probe_blockers.append(f"{path}: deleted")
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
        res.replica_ok = False
        return _config(path, old, _apply_or_none(fp, old), res)
    if not path.endswith(".py"):
        res.replica_ok = False
        if path.startswith("sqlglot/"):
            # Package data could be read while importing; the probe would see the old bytes.
            # (Nothing under the pinned sqlglot opens a data file at import, so data files
            # elsewhere, fixtures and docs, cannot change whether sqlglot imports.)
            res.probe_blockers.append(f"{path}: package data changed")
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
        res.probe_blockers.append(f"{path}: patch not applicable")
        res.replica_ok = False
        res.add(maps.imported(path), f"{path}: patch not applicable, all importers")
        res.nontrivial = True
        return
    if not path.startswith("sqlglot/"):
        res.replica_ok = False
        res.probe_blockers.append(f"{path}: Python outside sqlglot/ is not overlaid")
    elif not all(part.isidentifier() for part in _module_of(path).split(".")):
        res.probe_blockers.append(f"{path}: not an importable module name")
    res.patched[path] = new
    probe_on = pyast.literal_probe()
    if maps.reads(path):
        res.replica_ok = False  # some test reads this file as text
    sites = pyast.analyse(old, new, key=path)
    for site in sites:
        if site.kind == "call":
            if site.proof is not None and maps.swap_is_noop(path, site.proof):
                # Tests whose profile is incomplete may have seen other types there.
                res.add(maps.imported(path) & maps.typeprof_incomplete,
                        f"{path}: {site.proof[3]}->{site.proof[4]} at line {site.proof[0]} is a no-op "
                        f"for every profiled operand type")
                continue
            res.add(maps.call(path, site.lines), f"{path}: {site}")
            res.call_sites.append((path, site.lines))
        elif site.kind == "import":
            if path.startswith("sqlglot/"):
                res.import_files.add(path)
            else:
                res.replica_ok = False
            bits = None
            if import_refiner is not None:
                bits = import_refiner(path, old, new, site, maps)
            if bits is None:
                bits = maps.imported(path)
            elif probe_on:
                # A narrowing assumes the patched code imports cleanly; checked once per commit.
                res.conditional.append((bits, maps.imported(path), f"{path}: {site}"))
                continue
            res.add(bits, f"{path}: {site}")
        elif site.kind == "doc":
            res.replica_ok = False
            res.add(maps.reads(path), f"{path}: {site}")
        else:  # syntax or unknown
            res.probe_blockers.append(f"{path}: {site.why or site.kind}")
            res.replica_ok = False
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


# --- BR_LITERAL_PROBE: what a literal narrowing needs besides a clean import ---------------
#
# Audit of the pinned sqlglot (eb11aec4). The only code that runs while a class is being
# created and reads its attributes is:
#   * sqlglot.expressions.core.Expr.__init_subclass__: reads cls.arg_types, writes cls.key,
#     cls.required_args, cls.__doc__;
#   * sqlglot.tokens._TokenizerBase.__init_subclass__: reads cls.QUOTES, IDENTIFIERS, *_STRINGS,
#     STRING_ESCAPES, BYTE_STRING_ESCAPES, IDENTIFIER_ESCAPES, COMMENTS, HINT_START, KEYWORDS,
#     SINGLE_TOKENS; writes only cls._QUOTES, _IDENTIFIERS, _FORMAT_STRINGS, BYTE_STRING_ESCAPES
#     (a copy), _STRING_ESCAPES, _BYTE_STRING_ESCAPES, _IDENTIFIER_ESCAPES, _COMMENTS,
#     _KEYWORD_TRIE (all new objects; new_trie builds a fresh dict);
#   * sqlglot.dialects.dialect._Dialect.__new__ (the dialect metaclass): reads klass.<mappings>
#     and klass.tokenizer_class.<escapes, quotes, KEYWORDS, NUMERIC_ESCAPES>; writes derived
#     values on klass only (fresh dicts and tries); registers klass in _Dialect._classes under
#     its *name* (no attribute value involved); pops entries from gen_cls.TRANSFORMS, a dict
#     that may be inherited from (shared with) a parent generator.
# Nothing else in sqlglot defines __init_subclass__, __set_name__, __class_getitem__,
# __prepare__, __instancecheck__, __subclasscheck__ or a metaclass. ABCMeta (abc.ABC,
# collections.abc) only looks at attribute values for __isabstractmethod__, which no literal has.
_AUDITED_HOOK_READS = {("__init_subclass__", "cls"), ("__new__", "klass")}
_UNRECORDED_HOOKS = {"__set_name__", "__class_getitem__", "__prepare__", "__instancecheck__", "__subclasscheck__"}
# Bases outside sqlglot whose class machinery is plain for class-body literals.
_PLAIN_EXTERNAL = {"builtins:object", "builtins:Exception", "builtins:BaseException", "abc:ABC"}
# Base names whose class creation consumes the body (fields, members, protocol attributes).
_SPECIAL_BASES = {"NamedTuple", "TypedDict", "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "ReprEnum",
                  "AutoName", "Protocol", "Generic", "type", "ABCMeta", "EnumMeta", "EnumType"}
# The base Dialect builds its tokenizer from this when it names none: a change to it (or to a
# class it inherits from) reaches the class state of every dialect, not only of its users.
_DEFAULT_TOKENIZERS = ("sqlglot.tokens:Tokenizer",)


def _external_base(mro_key, maps):
    """A class in the MRO that the maps do not describe (class_info covers the pinned sqlglot)
    and that is not one of the plain external bases."""
    if mro_key in maps.class_info or mro_key in _PLAIN_EXTERNAL:
        return False
    return mro_key.split(":", 1)[0] != "collections.abc"


def probe_exclusion(site, path_keys, maps):
    """Why a class-body literal site must not narrow at all (BR_LITERAL_PROBE), or None.

    Each of these lets code other than the class's users see the change even when the
    import succeeds:
      * an annotated assignment, or an Assign<->AnnAssign conversion: the annotation is
        evaluated in the class body (on 3.13 a bad name raises NameError), lands in
        __annotations__, and is consumed by @dataclass, NamedTuple and TypedDict, whose
        generated __init__/__new__ and defaults reach callers that never name the field (and
        are invisible to the tracer, which only records receivers of sqlglot code);
      * a decorator (any: @dataclass, registries, @trait), class keywords (metaclass=...,
        __init_subclass__ arguments) or type parameters on any class of the path;
      * a base that is not a plain name chain (Generic[T], a call), or whose name is in the
        Enum, NamedTuple, TypedDict, Protocol, Generic or metaclass families;
      * an MRO (from the maps) with a class the maps do not describe (outside sqlglot) other
        than object, Exception, BaseException, abc.ABC and collections.abc: enum members,
        tuple/dict/str subclasses, typing machinery, metaclasses (type);
      * a class the default tokenizer inherits from (see _DEFAULT_TOKENIZERS).
    Non-Name assignment targets (`D["k"] = 1` can mutate an inherited, shared dict) are already
    not literal under the flag (pyast._literal_change).
    """
    if site.annotated:
        return "annotated assignment"
    heads = site.class_heads
    if heads is None or len(heads) != len(site.class_path):
        return "class header unknown"
    for name, (decorators, bases, keywords, type_params) in zip(site.class_path, heads):
        if decorators:
            return f"class {name} is decorated"
        if keywords or type_params:
            return f"class {name} has class keywords or type parameters"
        for base in bases:
            if base is None:
                return f"class {name} has a base that is not a plain name"
            if base.rsplit(".", 1)[-1] in _SPECIAL_BASES:
                return f"class {name} derives from {base}"
    for k in path_keys:
        info = maps.class_info.get(k)
        if info is None:
            return f"{k} unknown to the maps"
        for b in info[1]:
            if _external_base(b, maps):
                return f"{k} derives from {b}"
        for d in _DEFAULT_TOKENIZERS:
            if d in maps.class_info and k in maps.class_info[d][1]:
                return f"{k} feeds the default tokenizer of every dialect"
    return None


def refine_class_literal(path, old, new, site, maps):
    """Tests that can observe a literal change to a class attribute, or None to fall back.

    A class-body assignment that only swaps literal values cannot fail at import, and
    its new value is seen only through the class or its subclasses. Tests never touch
    those classes unless the tracer recorded an object or classmethod of one of them
    (receivers are recorded with their whole MRO) or the test ran code naming them.
    Falls back (None) whenever the value may also feed state built at import time.

    BR_LITERAL_PROBE=1. "Cannot fail at import" is false: a hook or metaclass consumes the
    value while the class is created (`arg_types = ["this"]` breaks Expr.__init_subclass__,
    `HEREDOC_STRINGS = [("$",)]` breaks _TokenizerBase.__init_subclass__), and no static check
    of the literal can tell, because the failure depends on its type, shape and content. With
    the flag the result is only a candidate: commit_impact uses it only if the patched code is
    seen to import cleanly (one probe per commit), else all importers. probe_exclusion() first
    rules out sites whose effect escapes the class even on a clean import.

    Why "class users (and readers)" then suffices, given a clean import of every sqlglot
    module some test imports, with every patched module overlaid:
      1. After import, the old and new processes differ only in objects derived from the
         changed value. The value lives in the class __dict__ and is reached by attribute
         lookup on the family, by name (name readers), or by a computed name (dynamic_readers).
      2. Import-time reads in class bodies: a subclass body (enclosing class in the family)
         stores what it derives on that subclass; `Other.NAME` is another class's attribute;
         a read through a string, an unknown expression or the family's own name falls back.
      3. Reads in code that runs at import: through self/cls of an ordinary function, an object
         of the family would have been recorded in classes_at_import (checked above), except
         for the hooks the tracer does not record (_UNRECORDED_HOOKS), which fall back. Reads in
         class-creation hooks are allowed only for the audited hooks and receivers
         (_AUDITED_HOOK_READS, see the audit above): they store what they derive on the class
         being created, which is in the family or, for _Dialect.__new__, is the dialect owning
         the tokenizer (point 4). Derived state never goes to module-level registries
         (_Dialect._classes holds classes keyed by name); caches keyed by class
         (generator._DISPATCH_CACHE) are filled at call time by code that reads the attribute;
         KEYWORDS/TOKENS merges build new dicts in subclass bodies. The one hook write to a
         possibly shared object, gen_cls.TRANSFORMS.pop, reads TRANSFORMS through gen_cls (not
         audited) and SUPPORTED_JSON_PATH_PARTS through a string: both fall back.
      4. A dialect derives class state (QUOTE_START, ESCAPED_SEQUENCES, SUPPORTS_COLUMN_JOIN_MARKS,
         ...) from its tokenizer_class, directly or through the tokenizer's derived _QUOTES etc.,
         and can be used without creating its tokenizer. Its tokenizer_class is its own nested
         Tokenizer or a fresh subclass of its first base's tokenizer_class. So a dialect whose
         tokenizer is in the family is a subclass of the class enclosing the changed one (a
         path key), of a class enclosing a family member, or of the base Dialect when the family
         contains the default tokenizer. The first two are in the users below (class users
         include subclasses); the last falls back (probe_exclusion).
    Holes this does not close (inherited from the v2 design): a family class reached without
    an object, a classmethod or its name being read (Dialect.classes["x"].ATTR, or a dialect's
    derived attribute read off a class never instantiated); a subclass body that stores
    derived values into an object outside the family; computed-name reads (vars(),
    getattr(cls, name)) executed while importing; _names_an_instance is a naming heuristic.
    """
    if not maps.has_class_data or site.kind != "import" or not site.literal or not site.class_path:
        return None
    names = site.names
    if not names or "*" in names or any(n.startswith("__") and n.endswith("__") for n in names):
        return None
    probe_on = pyast.literal_probe()
    module = _module_of(path)
    path_keys = [f"{module}:{'.'.join(site.class_path[:i])}" for i in range(1, len(site.class_path) + 1)]
    key = path_keys[-1]
    info = maps.class_info.get(key)
    if info is None or not info[0]:
        return None  # objects of this class are invisible to the tracer
    if any(k in maps.classes_at_import for k in path_keys):
        return None  # used while modules were importing: may feed import-time state
    if probe_on and probe_exclusion(site, path_keys, maps) is not None:
        return None
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
                if probe_on and owner in _UNRECORDED_HOOKS:
                    return None  # the tracer does not record the receivers of these hooks
                use_readers = False  # read through a receiver; none of this family existed at import
                continue
            if base in ('"', "?"):
                return None
            if other_class(base):
                continue
            if owner in _CLASS_HOOKS:
                if probe_on and (owner, base.split(".")[0]) not in _AUDITED_HOOK_READS:
                    return None  # e.g. gen_cls.TRANSFORMS in _Dialect.__new__: maybe a shared dict
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
            if probe_on and key in mro:
                # A family member nested in another class (a dialect's Tokenizer) feeds that
                # class's state through the dialect metaclass: its users count too (point 4).
                cmod, qual = ckey.split(":", 1)
                parts = qual.split(".")
                for i in range(1, len(parts)):
                    users |= maps.class_users(f"{cmod}:{'.'.join(parts[:i])}")
    for r in refs:
        users |= maps.name_readers(r)
    bits = maps.imported(path) & users
    if use_readers:
        readers = 0
        for n in names:
            readers |= maps.name_readers(n) | maps.dynamic_readers(n, family_key=key)
        bits &= readers
    return bits
