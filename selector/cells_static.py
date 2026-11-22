"""BR_CELLS, static half: which import-level sites may be judged by observing import-time state.

impact.resolve_cells imports the pinned and the patched sqlglot in fresh interpreters and
compares every class __dict__ entry and module global ("cells") of every sqlglot module some
test imports (selector/state_probe.py). Whatever a changed statement does INSIDE that module
graph is observed there, so its expressions need not be literal (unlike refine_class_literal).
What the probe cannot see is an effect OUTSIDE the graph: process state (the probe reports
env_changed for the recursion limit, os.environ, sys.modules, warnings filters, patched stdlib
attributes), state held by the standard library or third-party modules (loggers, codec and
copyreg registries, atexit, threads, files, caches keyed by value), code that runs only in the
test environment (site-packages present: `try: import mypy_extensions`; test modules imported)
and code that runs only under conditions the probe does not reproduce. judge() bounds each
changed statement so that it can do none of these:

  * a module- or class-body Assign/AugAssign/AnnAssign (changed, inserted or removed) whose
    targets are plain names that are not dunders; an Import/ImportFrom (not `__future__`, no
    dunder names); an undecorated, non-dunder def inserted or removed (its body does not run,
    its defaults and, without postponed annotations, its annotations do); or a def header
    whose only change is default values (pyast emits that import site only when a default is
    not a constant);
  * not inside a module- or class-level compound statement (if/try/for/with/match): such a
    statement may run only in the test environment, never in the probe child (-I -S);
  * every changed sub-expression, on both sides, and every unchanged expression that consumes
    it (its ancestors: a call taking a changed argument runs with new input), is built from
    constants, names, attribute chains (no dunder attribute other than __name__, __qualname__,
    __module__, __doc__), displays, unpacking, subscripts, f-strings, operators, conditional
    expressions, comprehensions, lambdas (their bodies are checked when a call could run them
    at import, otherwise only for banned names), and calls of: a side-effect-free builtin
    (_BUILTINS), a non-mutating method of builtin values (_SAFE_METHODS, called on any allowed
    expression), or a name/attribute chain that resolves through the module's own top-level
    bindings to a name under `sqlglot.` (impact.callee_problem then requires a def or class of
    the pinned tree that this commit does not change);
  * no exec/eval/__import__/compile/setattr/delattr/getattr/hasattr/globals/locals/vars/open/
    print/input/breakpoint/super/type/id/hash in the changed parts (lambda bodies included), no
    walrus, no await/yield;
  * a changed annotation needs `from __future__ import annotations` in both versions (then it is
    a string stored in __annotations__, never evaluated, and shows up as an `__annotations__`
    cell); an inserted/removed annotated statement without it must have an allowed annotation.

Builtins: set/frozenset/tuple/list/dict/sorted/reversed/len/str/int/float/bool/bytes/range/
enumerate/zip/min/max/sum/any/all/isinstance/issubclass/repr/abs/ord/chr/object build values from
their arguments and touch no global state; the dunders they call (__iter__, __lt__, __hash__,
__repr__, __len__, __instancecheck__) belong to argument objects, which are sqlglot or builtin
objects. `type` is left out (with three arguments it creates a class, and base.__subclasses__()
is state no cell holds); map/filter are left out (they would run a lambda whose body we would
have to treat as executed: simpler to refuse); getattr/hasattr are left out (a computed-name read
can reach a module __getattr__ outside sqlglot); `re.compile` is left out for now (re._cache is
harmless but it is outside the graph; see the hand-back).
"""

import ast
from difflib import SequenceMatcher

from .pyast import _absolute_module, _dotted, _strip_annotations, bound_names, dump, dump_field

_BUILTINS = frozenset({
    "set", "frozenset", "tuple", "list", "dict", "sorted", "reversed", "len", "str", "int", "float", "bool",
    "bytes", "range", "enumerate", "zip", "min", "max", "sum", "any", "all", "isinstance", "issubclass", "repr",
    "abs", "ord", "chr", "object", "dict.fromkeys", "str.join",
})
_SAFE_METHODS = frozenset({
    "copy", "keys", "values", "items", "get", "union", "intersection", "difference", "symmetric_difference",
    "issubset", "issuperset", "isdisjoint", "join", "upper", "lower", "strip", "lstrip", "rstrip", "split",
    "rsplit", "replace", "format", "startswith", "endswith", "title", "capitalize", "casefold", "count", "index",
    "removeprefix", "removesuffix", "splitlines", "partition", "rpartition", "zfill", "ljust", "rjust",
    "center", "isidentifier", "isupper", "islower", "isdigit", "isalpha", "isalnum", "fromkeys",
})
BANNED = frozenset({
    "exec", "eval", "__import__", "compile", "setattr", "delattr", "getattr", "hasattr", "globals", "locals",
    "vars", "open", "print", "input", "breakpoint", "super", "type", "id", "hash", "exit", "quit", "help",
    "__builtins__", "memoryview",
})
_OK_DUNDER_ATTRS = frozenset({"__name__", "__qualname__", "__module__", "__doc__"})
_EXPR = (ast.Constant, ast.Name, ast.Attribute, ast.Subscript, ast.Slice, ast.Tuple, ast.List, ast.Set,
         ast.Dict, ast.Starred, ast.JoinedStr, ast.FormattedValue, ast.BinOp, ast.UnaryOp, ast.BoolOp,
         ast.Compare, ast.IfExp, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
         ast.comprehension, ast.Lambda, ast.arguments, ast.arg, ast.keyword, ast.Call, ast.expr_context,
         ast.operator, ast.unaryop, ast.boolop, ast.cmpop) + ((ast.Index,) if hasattr(ast, "Index") else ())
_ANNOTATION = "annotation evaluated (no `from __future__ import annotations` in both versions)"


def has_future_annotations(tree):
    return any(isinstance(s, ast.ImportFrom) and s.module == "__future__"
               and any(a.name == "annotations" for a in s.names) for s in tree.body)


def is_dunder(name):
    return isinstance(name, str) and len(name) > 4 and name.startswith("__") and name.endswith("__")


def toplevel_bindings(tree):
    """(name -> [how the module binds it while importing], names rebound by `global`).

    Module code only: def and class bodies and lambdas are not entered. A binding is
    ("module", dotted) for `import a.b [as n]`, ("from", level, module, name), ("def",) for a
    def or class statement, ("other",) for anything else; "*" is a key when the module
    star-imports."""
    out = {}
    rebound = set()

    def bind(name, what):
        out.setdefault(name, []).append(what)

    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name, ("def",))
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Global, ast.Nonlocal)):
                    rebound.update(sub.names)
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    bind(a.asname, ("module", a.name))
                else:
                    top = a.name.split(".")[0]
                    bind(top, ("module", top))
            continue
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    bind("*", ("other",))
                else:
                    bind(a.asname or a.name, ("from", node.level, node.module, a.name))
            continue
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bind(node.id, ("other",))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bind(node.name, ("other",))
        elif isinstance(node, ast.Global):
            rebound.update(node.names)
        elif isinstance(node, (getattr(ast, "MatchAs", ()), getattr(ast, "MatchStar", ()))) and node.name:
            bind(node.name, ("other",))
        stack.extend(ast.iter_child_nodes(node))
    return out, rebound


class Ctx:
    """Name resolution for one side (old or new tree) of one site; collects sqlglot callees."""

    def __init__(self, module, is_package, tree, class_path, cache):
        self.module = module
        self.is_package = is_package
        self.callees = set()
        key = id(tree)
        if key not in cache:
            cache[key] = (tree, toplevel_bindings(tree))
        self.bindings, self.rebound = cache[key][1]
        self.class_path = class_path
        self.class_local = {}
        if class_path:
            body, node = tree.body, None
            for name in class_path:
                node = next((s for s in body if isinstance(s, ast.ClassDef) and s.name == name), None)
                if node is None:
                    break
                body = node.body
            if node is None:
                self.class_local = None  # the class is not where the path says: resolve nothing
            else:
                for stmt in node.body:
                    kind = "def" if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else "other"
                    for b in bound_names(stmt):
                        self.class_local.setdefault(b, []).append(kind)

    def resolve(self, dotted):
        """The absolute dotted name `dotted` (source text) refers to here, or None when unsure:
        bound more than once or by anything but an import/def/class, star imports, `global`."""
        import builtins

        if self.class_local is None or self.module is None:
            return None
        head, _, rest = dotted.partition(".")
        tail = f".{rest}" if rest else ""
        if self.class_path:
            local = self.class_local.get(head)
            if local is not None:
                if local == ["def"]:
                    return f"{self.module}.{'.'.join(self.class_path)}.{head}{tail}"
                return None
        if head in self.rebound or "*" in self.bindings:
            return None
        found = self.bindings.get(head)
        if found is None:
            return f"builtins.{dotted}" if hasattr(builtins, head) else None
        if len(found) != 1:
            return None
        what = found[0]
        if what[0] == "module":
            return what[1] + tail
        if what[0] == "def":
            return f"{self.module}.{head}{tail}"
        if what[0] == "from":
            base = _absolute_module(self.module, self.is_package, what[1], what[2])
            return f"{base}.{what[3]}{tail}" if base else None
        return None


def _targets_problem(targets):
    for t in targets:
        for node in ast.walk(t):
            if isinstance(node, ast.Name):
                if is_dunder(node.id):
                    return f"assigns dunder {node.id}"
            elif not isinstance(node, (ast.Tuple, ast.List, ast.Starred, ast.expr_context)):
                return f"assigns to a {type(node).__name__} target"
    return None


def _banned_in(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in BANNED:
            return f"uses {sub.id}"
    return None


def expr_problem(node, ctx, under_call):
    """Why evaluating `node` may act outside the snapshotted module graph, or None.
    under_call: a call may run the lambdas built here."""
    if node is None:
        return None
    if isinstance(node, ast.Lambda):
        p = _args_problem(node.args, ctx, under_call)
        if p:
            return p
        return expr_problem(node.body, ctx, True) if under_call else _banned_in(node.body)
    if isinstance(node, ast.NamedExpr):
        return "walrus"
    if isinstance(node, ast.Name):
        return f"uses {node.id}" if node.id in BANNED else None
    if isinstance(node, ast.Attribute):
        if is_dunder(node.attr) and node.attr not in _OK_DUNDER_ATTRS:
            return f"reads .{node.attr}"
        return expr_problem(node.value, ctx, under_call)
    if isinstance(node, ast.Call):
        p = _callee_problem(node.func, ctx)
        if p:
            return p
        for a in list(node.args) + [k.value for k in node.keywords]:
            p = expr_problem(a, ctx, True)
            if p:
                return p
        return None
    if not isinstance(node, _EXPR):
        return f"{type(node).__name__} expression"
    for child in ast.iter_child_nodes(node):
        p = expr_problem(child, ctx, under_call)
        if p:
            return p
    return None


def _args_problem(args, ctx, under_call):
    for d in list(args.defaults) + [d for d in args.kw_defaults if d is not None]:
        p = expr_problem(d, ctx, under_call)
        if p:
            return p
    return None


def _callee_problem(func, ctx):
    dotted = _dotted(func)
    if dotted is None:
        if isinstance(func, ast.Attribute) and func.attr in _SAFE_METHODS:
            return expr_problem(func.value, ctx, True)
        return "call of a computed callee"
    p = expr_problem(func, ctx, True)  # banned names, dunder attributes
    if p:
        return p
    resolved = ctx.resolve(dotted)
    if resolved is None:
        return f"call to {dotted} (unresolved)"
    safe = "." in dotted and dotted.rsplit(".", 1)[-1] in _SAFE_METHODS
    if resolved.startswith("builtins."):
        if resolved[len("builtins."):] in _BUILTINS or safe:
            return None
        return f"call to {dotted}"
    if resolved.split(".", 1)[0] != "sqlglot":
        return f"call to {resolved}"
    ctx.callees.add((resolved, safe))
    return None


def _entry_key(x):
    if isinstance(x, tuple):
        return tuple(dump(e) if isinstance(e, ast.AST) else repr(e) for e in x)
    return dump(x) if isinstance(x, ast.AST) else repr(x)


def changed_points(a, b, anc, out):
    """Append (old part or None, new part or None, ancestors as (old, new) pairs) for every place
    two expressions differ; unchanged siblings are not collected."""
    if a is None or b is None or type(a) is not type(b):
        if a is not None or b is not None:
            out.append((a, b, anc))
        return
    if not isinstance(a, ast.AST):
        if a != b:
            out.append((None, None, anc))
        return
    if dump(a) == dump(b):
        return
    for f in a._fields:
        va, vb = getattr(a, f, None), getattr(b, f, None)
        if not isinstance(va, (list, ast.AST)) and not isinstance(vb, (list, ast.AST)):
            if va != vb or type(va) is not type(vb):
                out.append((a, b, anc))  # an operator, a name, a constant: the node itself changed
                return
    here = anc + [(a, b)]
    if isinstance(a, ast.Dict):
        _changed_seq(list(zip(a.keys, a.values)), list(zip(b.keys, b.values)), here, out)
        return
    for f in a._fields:
        va, vb = getattr(a, f, None), getattr(b, f, None)
        if isinstance(va, list) and isinstance(vb, list):
            _changed_seq(va, vb, here, out)
        elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
            changed_points(va, vb, here, out)


def _changed_seq(la, lb, anc, out):
    def pair(x, y):
        if isinstance(x, tuple):  # dict entries (key or None for **, value)
            if (x[0] is None) != (y[0] is None):
                out.append((x[0] or x[1], None, anc))
                out.append((None, y[0] or y[1], anc))
                return
            if x[0] is not None:
                changed_points(x[0], y[0], anc, out)
            changed_points(x[1], y[1], anc, out)
        else:
            changed_points(x, y, anc, out)

    def one(x, side):
        for e in (x if isinstance(x, tuple) else (x,)):
            if e is not None:
                out.append((e, None, anc) if side == 0 else (None, e, anc))

    if len(la) == len(lb):
        for x, y in zip(la, lb):
            pair(x, y)
        return
    sm = SequenceMatcher(None, [_entry_key(x) for x in la], [_entry_key(y) for y in lb], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if i2 - i1 == j2 - j1:
            for x, y in zip(la[i1:i2], lb[j1:j2]):
                pair(x, y)
            continue
        for x in la[i1:i2]:
            one(x, 0)
        for y in lb[j1:j2]:
            one(y, 1)


def _point_problem(node, ancestors, ctx):
    """A changed part (None: nothing on this side) and the unchanged expressions consuming it,
    outermost first."""
    under_call = False
    for i, a in enumerate(ancestors):
        if isinstance(a, ast.Lambda):
            nxt = ancestors[i + 1] if i + 1 < len(ancestors) else node
            if nxt is a.body and not under_call:
                # a lambda body that no call can run at import: only the banned names matter
                return _banned_in(node) if node is not None else None
            continue
        if isinstance(a, ast.NamedExpr):
            return "walrus"
        if isinstance(a, ast.Call):
            p = _callee_problem(a.func, ctx)
            if p:
                return p
            under_call = True
            continue
        if isinstance(a, ast.Attribute) and is_dunder(a.attr) and a.attr not in _OK_DUNDER_ATTRS:
            return f"reads .{a.attr}"
        if not isinstance(a, _EXPR):
            return f"{type(a).__name__} expression"
    if node is None or isinstance(node, (ast.expr_context, ast.operator, ast.unaryop, ast.boolop, ast.cmpop)):
        return None
    return expr_problem(node, ctx, under_call)


def points_problem(o_expr, n_expr, octx, nctx):
    pts = []
    changed_points(o_expr, n_expr, [], pts)
    for a, b, anc in pts:
        p = _point_problem(a, [x for x, _ in anc], octx) or _point_problem(b, [y for _, y in anc], nctx)
        if p:
            return p
    return None


def _import_problem(stmt):
    if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
        return "__future__ import"
    for a in stmt.names:
        if a.name == "*":
            continue
        if is_dunder(a.asname or a.name.split(".")[0]) or is_dunder(a.name.rsplit(".", 1)[-1]):
            return f"imports dunder {a.name}"
    return None


def _postponed(analysis):
    return analysis.future_annotations and analysis.new_future_annotations


def _whole_stmt_problem(analysis, stmt, ctx):
    """An inserted or removed statement: all of it runs (or ran) at import."""
    if isinstance(stmt, ast.Assign):
        return _targets_problem(stmt.targets) or expr_problem(stmt.value, ctx, False)
    if isinstance(stmt, ast.AugAssign):
        return _targets_problem([stmt.target]) or expr_problem(stmt.value, ctx, False)
    if isinstance(stmt, ast.AnnAssign):
        if not stmt.simple:
            return "annotated non-name target"
        p = _targets_problem([stmt.target]) or expr_problem(stmt.value, ctx, False)
        if p is None and not _postponed(analysis):
            p = expr_problem(stmt.annotation, ctx, False)
            p = p and f"annotation: {p}"
        return p
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return _import_problem(stmt)
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if stmt.decorator_list:
            return "decorated def"
        if is_dunder(stmt.name):
            return f"def {stmt.name}"
        if getattr(stmt, "type_params", None):
            return "def with type parameters"
        p = _args_problem(stmt.args, ctx, False)
        if p:
            return p
        if not _postponed(analysis):
            a = stmt.args
            anns = [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs + [v for v in (a.vararg, a.kwarg) if v]
                    if x.annotation is not None] + ([stmt.returns] if stmt.returns is not None else [])
            for ann in anns:
                p = expr_problem(ann, ctx, False)
                if p:
                    return f"annotation: {p}"
        return None
    if isinstance(stmt, ast.ClassDef):
        return "class statement inserted or removed"
    return f"{type(stmt).__name__} statement"


def _shape(args):
    """A signature without its default values and annotations."""
    s = _strip_annotations(args)
    return s[:-6] + s[-4:] + [len(args.defaults), [d is None for d in args.kw_defaults]]


def _annotations(fn):
    a = fn.args
    return [dump_field(x, "annotation") for x in a.posonlyargs + a.args + a.kwonlyargs] + \
        [dump_field(a.vararg, "annotation") if a.vararg else "", dump_field(a.kwarg, "annotation") if a.kwarg else "",
         dump_field(fn, "returns")]


def _def_header_problem(analysis, o, n, octx, nctx):
    if dump_field(o, "decorator_list") != dump_field(n, "decorator_list"):
        return "decorators changed"
    if o.name != n.name or dump_field(o, "type_params") != dump_field(n, "type_params"):
        return "def name or type parameters changed"
    if _shape(o.args) != _shape(n.args):
        return "parameters changed"
    if _annotations(o) != _annotations(n) and not _postponed(analysis):
        return _ANNOTATION
    for x, y in zip(o.args.defaults + [d for d in o.args.kw_defaults if d is not None],
                    n.args.defaults + [d for d in n.args.kw_defaults if d is not None]):
        p = points_problem(x, y, octx, nctx)
        if p:
            return p
    return None


def _changed_stmt_problem(analysis, o, n, octx, nctx):
    if isinstance(o, ast.Assign):
        return _targets_problem(o.targets) or _targets_problem(n.targets) or \
            points_problem(o.value, n.value, octx, nctx)
    if isinstance(o, ast.AugAssign):
        p = _targets_problem([o.target]) or _targets_problem([n.target])
        if p:
            return p
        if type(o.op) is not type(n.op):
            return expr_problem(o.value, octx, False) or expr_problem(n.value, nctx, False)
        return points_problem(o.value, n.value, octx, nctx)
    if isinstance(o, ast.AnnAssign):
        if not (o.simple and n.simple):
            return "annotated non-name target"
        p = _targets_problem([o.target]) or _targets_problem([n.target])
        if p:
            return p
        if dump(o.annotation) != dump(n.annotation) and not _postponed(analysis):
            return _ANNOTATION
        return points_problem(o.value, n.value, octx, nctx)
    if isinstance(o, (ast.Import, ast.ImportFrom)):
        return _import_problem(o) or _import_problem(n)
    return f"{type(o).__name__} statement"


def judge(analysis, old_tree, new_tree):
    """Set state_ok / state_why / state_callees on every pending import site of an Analysis."""
    cache = {}
    for site, how, o, n, scope, nested in analysis._pending:
        site.state_ok, site.state_callees = False, ()
        if analysis.module is None:
            why = "module unknown"
        elif how == "header":
            why = site.why or "header"
        elif nested:
            why = "inside a module- or class-level compound statement"
        elif scope[0] == "class" and (len(scope) < 3 or scope[2] is None):
            why = "class created inside a function"
        elif scope[0] not in ("module", "class"):
            why = "not module or class level"
        else:
            cp = scope[2] if scope[0] == "class" else None
            octx = Ctx(analysis.module, analysis.is_package, old_tree, cp, cache)
            nctx = Ctx(analysis.module, analysis.is_package, new_tree, cp, cache)
            if how == "removed":
                why = _whole_stmt_problem(analysis, o, octx)
            elif how == "inserted":
                why = _whole_stmt_problem(analysis, n, nctx)
            elif how == "def":
                why = _def_header_problem(analysis, o, n, octx, nctx)
            else:
                why = _changed_stmt_problem(analysis, o, n, octx, nctx)
            if not why:
                site.state_callees = tuple(sorted(octx.callees | nctx.callees))
        site.state_ok = not why
        site.state_why = why or "eligible"
