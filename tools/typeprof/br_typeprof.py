import ast
import atexit
import builtins
import enum
import importlib.abc
import importlib.machinery
import json
import os
import sys

_OUT = os.environ.get("BR_TYPEPROF_DIR", "")
_REPO = os.environ.get("BR_REPO", "").rstrip("/") + "/"
_seen = {}  # site -> set of (left type, right type)
_written = False

# Custom __eq__ methods checked by hand at the pinned commit: they return False for
# None (no exception, no side effect), so `x == None` agrees with `x is None`.
NONE_SAFE_EQ = {
    "sqlglot.expressions.core:Expr.__eq__",
    "sqlglot.dialects.dialect:_Dialect.__eq__",
    "sqlglot.dialects.dialect:Dialect.__eq__",
}


def _impl(tp, name):
    for k in tp.__mro__:
        if name in vars(k):
            return k, vars(k)[name]
    return object, getattr(object, name)


def classify(tp):
    """How `==`/`!=` behave for objects of this type.

    none     NoneType
    ident    __eq__ and __ne__ are object's: equality is identity
    enum     an Enum whose __eq__/__ne__ come from object/Enum or a builtin mixin:
             two members of the same class are equal only if identical
    builtin  __eq__ from a builtin type (int, str, tuple, ...): never equal to None
    nonesafe a hand-checked custom __eq__ that is False for None
    other    anything else
    """
    if tp is type(None):
        return "none"
    eq_owner, eq = _impl(tp, "__eq__")
    ne_owner, ne = _impl(tp, "__ne__")
    if eq is object.__eq__ and ne is object.__ne__:
        return "ident"
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        if getattr(eq_owner, "__module__", "") in ("builtins", "enum") and \
                getattr(ne_owner, "__module__", "") in ("builtins", "enum"):
            return "enum"
        return "other"
    if getattr(eq_owner, "__module__", "") == "builtins" and getattr(ne_owner, "__module__", "") == "builtins":
        return "builtin"
    key = f"{getattr(eq_owner, '__module__', '?')}:{getattr(eq, '__qualname__', '?')}"
    if key in NONE_SAFE_EQ and (ne is object.__ne__ or getattr(ne_owner, "__module__", "") == "builtins"
                                or f"{ne_owner.__module__}:{ne.__qualname__}" == "sqlglot.expressions.core:Expr.__ne__"):
        return "nonesafe"
    return "other"


def _key(tp):
    return f"{getattr(tp, '__module__', '?')}:{getattr(tp, '__qualname__', getattr(tp, '__name__', '?'))}"


def __br_cmp__(site, op, a, b):
    pair = (type(a), type(b))
    s = _seen.get(site)
    if s is None:
        _seen[site] = {pair}
    elif pair not in s:
        s.add(pair)
    if op == 0:
        return a is b
    if op == 1:
        return a is not b
    if op == 2:
        return a == b
    return a != b


_OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3}


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in _OPS:
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        call = ast.Call(
            func=ast.Name(id="__br_cmp__", ctx=ast.Load()),
            args=[ast.Constant(site), ast.Constant(_OPS[type(node.ops[0])]), node.left, node.comparators[0]],
            keywords=[],
        )
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        source = self.get_data(path)
        tree = ast.parse(source, filename=path)
        rel = path[len(_REPO):] if path.startswith(_REPO) else path
        tree = ast.fix_missing_locations(_Rewrite(rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return spec
        if not spec.origin.startswith(_REPO + "sqlglot/"):
            return spec
        spec.loader = _Loader(fullname, spec.origin)
        return spec


def _write():
    global _written
    if _written or not _OUT:
        return
    _written = True
    data = {}
    cats = {}
    for site, pairs in _seen.items():
        rows = []
        for a, b in pairs:
            for t in (a, b):
                if t not in cats:
                    try:
                        cats[t] = classify(t)
                    except Exception:
                        cats[t] = "other"
            rows.append([_key(a), cats[a], _key(b), cats[b]])
        data[site] = rows
    tmp = os.path.join(_OUT, f".{os.getpid()}.tmp")
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, os.path.join(_OUT, f"{os.getpid()}.json"))


def install():
    sys.dont_write_bytecode = True
    builtins.__br_cmp__ = __br_cmp__
    sys.meta_path.insert(0, _Finder())
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
