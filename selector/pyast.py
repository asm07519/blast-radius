"""Turn an old/new pair of Python sources into change sites.

A site says where the old program can first behave differently:

  Site("call", lines)    code inside a function, lambda or generator body changed;
                         a test can only be affected if it ran one of these old lines
                         inside such a code object.
  Site("import", lines, names)
                         module- or class-level code changed; it runs when the module
                         is imported.
  Site("doc", lines)     a docstring changed.
  Site("syntax")         the new file does not compile, or cannot be analysed.

Statements are matched by their exact source text first, so comments and blank
lines between statements never produce a site; statements whose text differs are
compared by AST, which ignores comments and formatting inside them.
"""

import ast
import os
import re
from difflib import SequenceMatcher

_COOKIE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


class Site:
    __slots__ = ("kind", "lines", "names", "why", "class_path", "literal", "proof", "annotated", "class_heads",
                 "value_only", "excluded", "state_ok", "state_why", "state_callees")

    def __init__(self, kind, lines=(), names=(), why="", class_path=None, literal=False, proof=None,
                 annotated=False, class_heads=None, value_only=False):
        self.kind = kind
        self.lines = set(lines)
        self.names = set(names)
        self.why = why
        self.class_path = class_path  # enclosing classes from module level, for class-body changes
        self.literal = literal  # the change only swaps literal values, so running it cannot raise
        # (line, col, end_line, old_op, new_op) when the only difference is one comparison
        # swapping `is`<->`==` or `is not`<->`!=`; the selector may prove it a no-op.
        self.proof = proof
        # The changed, inserted or removed statement is an annotated assignment (or one side of
        # an Assign<->AnnAssign conversion): its annotation is evaluated, and @dataclass /
        # NamedTuple / TypedDict consume annotated names.
        self.annotated = annotated
        # For class-body sites: one ClassHead per class on class_path (outermost first).
        self.class_heads = class_heads
        # An annotated assignment changed in place: same simple Name target, the same annotation
        # (a plain type expression that does not read the target), only the value differs
        # (see value_only_annassign; impact.probe_exclusion lets these narrow).
        self.value_only = value_only
        # Why impact.probe_exclusion refused to narrow this site (for the commit's notes), or None.
        self.excluded = None
        # BR_CELLS (import sites only, set by Analysis when the flag is on): the statement may be
        # judged by observing import-time state (state_eligibility), why not, and the dotted
        # sqlglot callees its changed parts call (impact checks they are defined and unchanged).
        self.state_ok = False
        self.state_why = "not judged"
        self.state_callees = ()

    def __repr__(self):
        ls = sorted(self.lines)
        span = f"{ls[0]}-{ls[-1]}({len(ls)})" if ls else "-"
        return f"Site({self.kind}, {span}, {sorted(self.names)[:5]}, {self.why})"


_dump_cache = {}


def dump(node):
    key = id(node)
    hit = _dump_cache.get(key)
    if hit is not None and hit[0] is node:
        return hit[1]
    value = ast.dump(node, annotate_fields=False, include_attributes=False)
    _dump_cache[key] = (node, value)
    return value


def _first_line(node):
    decos = getattr(node, "decorator_list", None)
    if decos:
        return min(min(d.lineno for d in decos), node.lineno)
    return node.lineno


def span(node):
    return set(range(_first_line(node), (node.end_lineno or node.lineno) + 1))


def _is_const_expr(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


def _is_doc(node):
    return _is_const_expr(node) and isinstance(node.value.value, str)


def entry_lines(stmt):
    """Lines that run whenever stmt starts executing."""
    if isinstance(stmt, (ast.If, ast.While)):
        return set(range(stmt.lineno, stmt.test.end_lineno + 1))
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return set(range(stmt.lineno, stmt.iter.end_lineno + 1))
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return set(range(stmt.lineno, stmt.items[-1].context_expr.end_lineno + 1))
    if isinstance(stmt, (ast.Try, getattr(ast, "TryStar", ast.Try))):
        return {stmt.lineno} | entry_lines(stmt.body[0])
    if isinstance(stmt, ast.Match):
        return set(range(stmt.lineno, stmt.subject.end_lineno + 1))
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return set(range(_first_line(stmt), stmt.lineno + 1))
    return span(stmt)


def _header_lines(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        end = node.body[0].lineno - 1 if node.body[0].lineno > node.lineno else node.lineno
        return set(range(_first_line(node), end + 1))
    return entry_lines(node)


def bound_names(stmt):
    """Names a module- or class-level statement binds (best effort)."""
    names = set()
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(stmt.name)
        return names
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
        targets = [stmt.target]
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            names.add((a.asname or a.name).split(".")[0])
        return names
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        targets = [stmt.target]
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
    if not names:
        names.add("*")  # unknown effect
    return names


_BIG = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For, ast.AsyncFor, ast.While,
        ast.With, ast.AsyncWith, ast.Try, ast.Match) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())


class Analysis:
    def __init__(self, old_src, new_src, module=None, is_package=False):
        self.old_src = old_src
        self.new_src = new_src
        self.old_lines = old_src.splitlines(keepends=True)
        self.new_lines = new_src.splitlines(keepends=True)
        self.sites = []
        self.future_annotations = False
        # BR_CELLS: import sites waiting for state_eligibility, and how deep the current body
        # sits in module- or class-level compound statements (if/try/for/with/match).
        self.module = module
        self.is_package = is_package
        self.state = cells()
        self.new_future_annotations = False
        self._pending = []
        self._nested = 0

    def _pend(self, site, how, o, n, scope):
        if self.state and site.kind == "import":
            self._pending.append((site, how, o, n, scope, self._nested))

    # -- source text of a statement, decorators included
    def _text(self, lines, node):
        sl = _first_line(node)
        el = node.end_lineno
        if sl == node.lineno and not getattr(node, "decorator_list", None):
            sc = node.col_offset
        else:
            sc = 0
        ec = node.end_col_offset
        if sl == el:
            return lines[sl - 1][sc:ec]
        return lines[sl - 1][sc:] + b"".join(lines[sl:el - 1]) + lines[el - 1][:ec]

    def run(self, old_tree=None):
        if old_tree is None:
            try:
                old_tree = ast.parse(self.old_src)
            except (SyntaxError, ValueError):
                self.sites.append(Site("syntax", why="old file does not parse"))
                return self.sites
        try:
            new_tree = ast.parse(self.new_src)
        except (SyntaxError, ValueError, RecursionError):
            self.sites.append(Site("syntax", why="new file does not parse"))
            return self.sites
        self.future_annotations = any(
            isinstance(s, ast.ImportFrom) and s.module == "__future__"
            and any(a.name == "annotations" for a in s.names)
            for s in old_tree.body
        )
        if self._cookie(self.old_lines) != self._cookie(self.new_lines):
            self.sites.append(Site("syntax", why="coding cookie changed"))
            return self.sites
        try:
            self.diff_body(old_tree.body, new_tree.body, ("module", None), None)
        except RecursionError:
            self.sites.append(Site("syntax", why="too deep"))
        if self.state:
            from . import cells_static

            self.new_future_annotations = cells_static.has_future_annotations(new_tree)
            try:
                cells_static.judge(self, old_tree, new_tree)
            except RecursionError:
                for site, *_ in self._pending:
                    site.state_ok, site.state_why = False, "too deep to judge"
            except Exception as error:  # noqa: BLE001 - a judge that fails proves nothing
                for site, *_ in self._pending:
                    site.state_ok, site.state_why = False, f"judge failed ({error!r})"
            self._pending = []
        return self.sites

    @staticmethod
    def _cookie(lines):
        for line in lines[:2]:
            m = _COOKIE.match(line)
            if m:
                return m.group(1).lower()
        return None

    # -- bodies
    def diff_body(self, olds, news, scope, header):
        """header: entry lines of the block owning this body (used for insertions at its start)."""
        ot = [self._text(self.old_lines, s) for s in olds]
        nt = [self._text(self.new_lines, s) for s in news]
        if ot == nt:
            return
        sm = SequenceMatcher(None, ot, nt, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            o_part, n_part = olds[i1:i2], news[j1:j2]
            if len(o_part) != len(n_part) and not any(isinstance(x, _BIG) for x in o_part + n_part):
                # e.g. a statement split over more lines: same AST, different text
                if [dump(x) for x in o_part] == [dump(x) for x in n_part]:
                    continue
            k = min(len(o_part), len(n_part))
            for idx in range(k):
                o, n = o_part[idx], n_part[idx]
                if type(o) is type(n):
                    self.diff_stmt(o, n, scope, olds, i1 + idx, header)
                else:
                    # e.g. `X = 1` -> `X: int = 1`: both halves count as annotated
                    conv = isinstance(o, ast.AnnAssign) or isinstance(n, ast.AnnAssign)
                    self.removed(o, scope, olds, i1 + idx, header, annotated=conv)
                    self.inserted(n, scope, olds, i1 + idx, header, at_start=(i1 + idx == 0), annotated=conv)
            for idx in range(k, len(o_part)):
                self.removed(o_part[idx], scope, olds, i1 + idx, header)
            for idx in range(k, len(n_part)):
                self.inserted(n_part[idx], scope, olds, i1 + k, header, at_start=(i1 + k == 0 and j1 + idx == 0))

    def _neighbour_lines(self, olds, index, header):
        """Lines that run whenever control reaches position `index` of this body.

        The previous statement (it ran, so control moved on) and the next one. The block
        header is used only when there is neither: a def line runs where the function is
        defined, not where it is called, so it would over-select.
        """
        lines = set()
        if index > 0:
            lines |= entry_lines(olds[index - 1])
        if index < len(olds):
            lines |= entry_lines(olds[index])
        if not lines and header:
            lines |= header
        return lines

    def _own_lines(self, o, lines, olds, index, header):
        """A changed statement runs exactly when one of its own lines runs, unless it
        compiles to no line event (pass, global, nonlocal, a bare constant)."""
        if isinstance(o, (ast.Pass, ast.Global, ast.Nonlocal)) or _is_const_expr(o):
            return lines | self._neighbour_lines(olds, index, header)
        return lines

    def _site_for_scope(self, scope, lines, names, why, proof=None):
        kind = scope[0]
        if kind == "function":
            self.sites.append(Site("call", lines, why=why, proof=proof))
        else:
            self.sites.append(Site("import", lines, names, why=why))
            self._pend(self.sites[-1], "header", None, None, scope)

    def removed(self, o, scope, olds, index, header, annotated=False):
        if _is_const_expr(o):
            if index == 0 and _is_doc(o) and scope[0] != "function_body_nodoc":
                self.sites.append(Site("doc", span(o), why="docstring removed"))
            return
        if isinstance(o, ast.Pass):
            return
        if scope[0] == "function":
            self.sites.append(Site("call", self._own_lines(o, span(o), olds, index, header), why="stmt removed"))
        else:
            self.sites.append(Site("import", span(o), bound_names(o), why="stmt removed",
                                   class_path=_class_path(scope), literal=_literal_stmt(o),
                                   annotated=annotated or isinstance(o, ast.AnnAssign),
                                   class_heads=_class_heads(scope)))
            self._pend(self.sites[-1], "removed", o, None, scope)
            if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.sites.append(Site("call", span(o), why="def removed"))

    def inserted(self, n, scope, olds, index, header, at_start, annotated=False):
        if _is_const_expr(n):
            if at_start and _is_doc(n):
                self.sites.append(Site("doc", self._neighbour_lines(olds, index, header), why="docstring added"))
            return
        if isinstance(n, ast.Pass):
            return
        lines = self._neighbour_lines(olds, index, header)
        if scope[0] == "function":
            self.sites.append(Site("call", lines, why="stmt inserted"))
        else:
            self.sites.append(Site("import", lines, bound_names(n), why="stmt inserted",
                                   class_path=_class_path(scope), literal=_literal_stmt(n),
                                   annotated=annotated or isinstance(n, ast.AnnAssign),
                                   class_heads=_class_heads(scope)))
            self._pend(self.sites[-1], "inserted", None, n, scope)

    # -- statements of the same type
    def diff_stmt(self, o, n, scope, olds, index, header):
        if not isinstance(o, _BIG) and dump(o) == dump(n):
            return
        if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return self._diff_def(o, n, scope, olds, index, header)
        if isinstance(o, ast.ClassDef):
            hdr = ("name", "bases", "keywords", "decorator_list", "type_params")
            if any(dump_field(o, f) != dump_field(n, f) for f in hdr):
                self._site_for_scope(scope, _header_lines(o), {o.name, "*"}, "class header")
            if scope[0] == "module":
                path, heads = [o.name], (class_head(o),)
            elif scope[0] == "class" and len(scope) > 2 and scope[2] is not None:
                path, heads = scope[2] + [o.name], scope[3] + (class_head(o),)
            else:
                path, heads = None, None  # class created inside a function
            self.diff_body(o.body, n.body, ("class", o, path, heads), _header_lines(o))
            return
        compound = _COMPOUND.get(type(o))
        if compound is not None:
            hdr_fields, body_fields = compound
            changed_hdr = [f for f in hdr_fields if dump_field(o, f) != dump_field(n, f)]
            if changed_hdr:
                proof = None
                if scope[0] == "function" and len(changed_hdr) == 1:
                    proof = cmp_swap(getattr(o, changed_hdr[0]), getattr(n, changed_hdr[0]))
                self._site_for_scope(scope, entry_lines(o), bound_names(o), f"{type(o).__name__} header",
                                     proof=proof)
            self._nested += 1
            try:
                self._diff_compound_bodies(o, n, scope, body_fields)
            finally:
                self._nested -= 1
            return
        self._diff_simple(o, n, scope, olds, index, header)

    def _diff_compound_bodies(self, o, n, scope, body_fields):
        for f in body_fields:
            ob, nb = getattr(o, f), getattr(n, f)
            block_hdr = entry_lines(o)
            if f == "orelse" and ob:
                block_hdr = block_hdr | set(range(ob[0].lineno, ob[0].lineno + 1))
            if f == "handlers":
                self._diff_handlers(ob, nb, scope, o)
            elif f == "cases":
                self._diff_cases(ob, nb, scope, o)
            elif not ob or not nb:
                if dump_list(ob) != dump_list(nb):
                    self._site_for_scope(scope, entry_lines(o), bound_names(o), f"{f} added/removed")
            else:
                self.diff_body(ob, nb, scope, block_hdr)

    def _diff_simple(self, o, n, scope, olds, index, header):
        # simple statement
        if _is_const_expr(o) and _is_const_expr(n):
            if index == 0 and (_is_doc(o) or _is_doc(n)):
                self.sites.append(Site("doc", span(o), why="docstring changed"))
            return
        if scope[0] == "function":
            self.sites.append(Site("call", self._own_lines(o, span(o), olds, index, header), why="stmt changed",
                                   proof=cmp_swap(o, n)))
            return
        points = []
        _diff_points(o, n, None, points)
        if points and all(owner is not None for owner, _ in points):
            lines = set()
            for owner, _ in points:
                lines |= span(owner)
            self.sites.append(Site("call", lines, why="lambda/genexpr body changed"))
            return
        self.sites.append(Site("import", span(o), bound_names(o), why="module/class stmt changed",
                               class_path=_class_path(scope), literal=_literal_change(o, n),
                               annotated=isinstance(o, ast.AnnAssign) or isinstance(n, ast.AnnAssign),
                               class_heads=_class_heads(scope), value_only=value_only_annassign(o, n)))
        self._pend(self.sites[-1], "changed", o, n, scope)

    def _diff_def(self, o, n, scope, olds, index, header):
        sig_changed = dump_field(o, "args") != dump_field(n, "args")
        deco_changed = dump_field(o, "decorator_list") != dump_field(n, "decorator_list")
        name_changed = o.name != n.name
        ret_changed = dump_field(o, "returns") != dump_field(n, "returns")
        tp_changed = dump_field(o, "type_params") != dump_field(n, "type_params")
        if sig_changed and self.future_annotations and _strip_annotations(o.args) == _strip_annotations(n.args):
            sig_changed = False  # only annotations changed; they are never evaluated
        if ret_changed and not self.future_annotations:
            sig_changed = True
        if sig_changed or deco_changed or name_changed or tp_changed:
            # every caller of the function may now behave differently
            self.sites.append(Site("call", span(o), why=f"def {o.name} signature"))
            if scope[0] == "function":
                self.sites.append(Site("call", _header_lines(o) | self._neighbour_lines(olds, index, header),
                                       why="nested def header"))
            elif deco_changed or name_changed or tp_changed or not _only_constant_defaults(o.args, n.args):
                self.sites.append(Site("import", _header_lines(o), {o.name, n.name}, why=f"def {o.name} header"))
                self._pend(self.sites[-1], "def", o, n, scope)
        self.diff_body(o.body, n.body, ("function", o), _header_lines(o))


_SWAPS = {(ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)}


def cmp_swap(a, b):
    """(line, col, end_line, old_op, new_op) if a and b differ only by one is/== or is not/!= swap."""
    found = []

    def walk(x, y):
        if type(x) is not type(y):
            return False
        if isinstance(x, ast.Compare) and len(x.ops) == 1 and len(y.ops) == 1 \
                and type(x.ops[0]) is not type(y.ops[0]):
            if (type(x.ops[0]), type(y.ops[0])) in _SWAPS and dump(x.left) == dump(y.left) \
                    and dump(x.comparators[0]) == dump(y.comparators[0]):
                found.append((x.lineno, x.col_offset, x.end_lineno, type(x.ops[0]).__name__,
                              type(y.ops[0]).__name__))
                return True
            return False
        if isinstance(x, ast.AST):
            for f in x._fields:
                va, vb = getattr(x, f, None), getattr(y, f, None)
                if isinstance(va, list):
                    if not isinstance(vb, list) or len(va) != len(vb):
                        return False
                    if not all(walk(p, q) for p, q in zip(va, vb)):
                        return False
                elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
                    if va is None or vb is None or not walk(va, vb):
                        return False
                elif va != vb or type(va) is not type(vb):
                    return False
            return True
        return x == y

    try:
        ok = walk(a, b)
    except RecursionError:
        return None
    return found[0] if ok and len(found) == 1 else None


def _class_path(scope):
    if scope[0] == "class" and len(scope) > 2:
        return scope[2]
    return None


def _class_heads(scope):
    if scope[0] == "class" and len(scope) > 3:
        return scope[3]
    return None


def _dotted(node):
    """`a.b.C` for a plain name chain, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def class_head(node):
    """What a class statement passes to class creation, besides its body:
    (number of decorators, bases, keywords, has type parameters). Each base is a dotted name
    such as "exp.Expression", or None when it is not a plain name chain (a call, a subscript
    like Generic[T], a starred expression); keywords are (arg, dotted value or None), arg None
    for **kwargs."""
    return (len(node.decorator_list), tuple(_dotted(b) for b in node.bases),
            tuple((k.arg, _dotted(k.value)) for k in node.keywords), bool(getattr(node, "type_params", None)))


_CONTAINERS = (ast.Dict, ast.List, ast.Tuple, ast.Set)


def _pure_literal(node):
    """A tree that only builds constants and containers of constants: evaluating it cannot raise."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
        return _pure_literal(node.operand)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_pure_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _pure_literal(k) for k in node.keys) and all(_pure_literal(v) for v in node.values)
    return False


def literal_guard():
    """BR_LITERAL_GUARD=1: literal narrowing also needs ast.literal_eval to succeed (PRD §6 rule 5).

    Read on every call so tests can toggle it within one process.
    """
    return os.environ.get("BR_LITERAL_GUARD") == "1"


def literal_probe():
    """BR_LITERAL_PROBE=1: literal narrowing is only used after the patched code is seen to
    import cleanly, with static exclusions first (see impact.refine_class_literal).

    Read on every call so tests can toggle it within one process. Independent of literal_guard.
    """
    return os.environ.get("BR_LITERAL_PROBE") == "1"


def cells():
    """BR_CELLS=1: eligible import-level sites are judged by the import-time state ("cells") that
    differs between the pinned and the patched code (selector/cells_static.py decides which
    sites are eligible, impact.resolve_cells applies the rule). Read on every call."""
    return os.environ.get("BR_CELLS") == "1"


def _evaluates(node, hashable=False):
    """ast.literal_eval builds this exact value without raising (and, when it must be
    hashable because it is a dict key or set element, hashing it does not raise).

    literal_eval is stricter than Python (it rejects `not`, `- -1`, `-True`), which only
    costs a fallback. Any exception, RecursionError and MemoryError included, means no.
    """
    try:
        value = ast.literal_eval(node)
        if hashable:
            hash(value)
    except Exception:
        return False
    return True


def _literal_stmt(stmt):
    """An assignment of a pure literal to plain names."""
    guard = literal_guard()
    if isinstance(stmt, ast.Assign):
        return all(isinstance(t, ast.Name) for t in stmt.targets) and _pure_literal(stmt.value) \
            and (not guard or _evaluates(stmt.value))
    if isinstance(stmt, ast.AnnAssign):
        return isinstance(stmt.target, ast.Name) and stmt.value is not None and _pure_literal(stmt.value) \
            and (not guard or _evaluates(stmt.value))
    return False


def _literal_change(o, n):
    """o and n differ only in literal values (or in added/removed literal container items)."""
    if type(o) is not type(n):
        return _pure_literal(o) and _pure_literal(n)
    if isinstance(o, (ast.Assign, ast.AnnAssign)):
        if dump_field(o, "targets" if isinstance(o, ast.Assign) else "target") != \
                dump_field(n, "targets" if isinstance(n, ast.Assign) else "target"):
            return False
        if literal_probe() and not all(isinstance(t, ast.Name) for t in (o.targets if isinstance(o, ast.Assign)
                                                                         else [o.target])):
            # `D["k"] = 1`, `x.a = 1`, `A, B = 1, 2`: the store may mutate an object shared with other
            # classes (an inherited dict), so the change is not confined to this class's users.
            return False
        if isinstance(o, ast.AnnAssign) and dump_field(o, "annotation") != dump_field(n, "annotation"):
            return False
        if o.value is None or n.value is None:
            return False
        if literal_guard():
            # a RecursionError propagates, as from _literal_expr: Analysis.run turns it into "too deep"
            return _guarded_literal_expr(o.value, n.value, False)
        return _literal_expr(o.value, n.value)
    return False


# Nodes of a plain type expression: `t.ClassVar`, `str | None`, `t.ClassVar[dict[str, tuple[str, ...]]]`,
# `"Forward"`. No call, lambda, walrus, comprehension or starred: evaluating one only looks names
# up, takes attributes, subscripts typing objects and builds unions.
_TYPE_EXPR_NODES = (ast.Name, ast.Attribute, ast.Subscript, ast.Constant, ast.Tuple, ast.List, ast.BinOp, ast.BitOr,
                    ast.Load)


def value_only_annassign(o, n):
    """o and n are annotated assignments that differ at most in their value: the same simple Name
    target, the same annotation, and that annotation is a plain type expression that does not
    read the target (CPython stores the value first and evaluates the annotation after it, so
    `X: X = 1` would see the new value). Whether the values differ only in literals is
    _literal_change's question."""
    if not (isinstance(o, ast.AnnAssign) and isinstance(n, ast.AnnAssign)):
        return False
    if not (isinstance(o.target, ast.Name) and isinstance(n.target, ast.Name) and o.target.id == n.target.id):
        return False
    if o.simple != 1 or n.simple != 1 or o.value is None or n.value is None:
        return False
    if dump(o.annotation) != dump(n.annotation):
        return False
    for node in ast.walk(o.annotation):
        if not isinstance(node, _TYPE_EXPR_NODES):
            return False
        if isinstance(node, ast.BinOp) and not isinstance(node.op, ast.BitOr):
            return False
        if isinstance(node, ast.Name) and node.id == o.target.id:
            return False
    return True


# --- class statements of a whole module (impact.probe_exclusion's subclass check) ------------

class ClassFacts:
    """What one class statement passes to class creation besides its body, with names resolved
    through the module's imports.

    qualname: "A.B" for a class at module level or nested in classes (if/try blocks included),
              None for a class created inside a function.
    decorators: (source text, resolved dotted name or None, is a call) per decorator.
    bases: (dotted source name or None, resolved dotted name or None) per base.
    keywords, type_params: whether the statement has any.
    """

    __slots__ = ("qualname", "lineno", "decorators", "bases", "keywords", "type_params")

    def __init__(self, qualname, lineno, decorators, bases, keywords, type_params):
        self.qualname = qualname
        self.lineno = lineno
        self.decorators = decorators
        self.bases = bases
        self.keywords = keywords
        self.type_params = type_params


_CLASS_LINE = re.compile(rb"class[ \t]+([A-Za-z_][A-Za-z0-9_]*)")
_SIMPLE_CLASS_HEADER = re.compile(rb"class[ \t]+[A-Za-z_][A-Za-z0-9_]*[ \t]*(?:\(([ \t]*(?:[A-Za-z_][A-Za-z0-9_.]*[ \t]*"
                                  rb"(?:,[ \t]*[A-Za-z_][A-Za-z0-9_.]*[ \t]*)*,?)?[ \t]*)\))?[ \t]*:")
_STRING_ON_LINE = re.compile(rb"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"")
_DEF_START = re.compile(rb"(?:def|async)[ \t]")
_AS_NAME = re.compile(rb"\bas[ \t]+([A-Za-z_][A-Za-z0-9_]*)")


def _has_code(line):
    s = line.strip(b" \t\f\r")
    return bool(s) and not s.startswith(b"#")


def _code_depth(text, depth):
    """Bracket depth after `text` (one physical line), starting from `depth`, or None when the
    line has a string that does not end on it, a backslash continuation, or closes too much."""
    code = _STRING_ON_LINE.sub(b"", text)
    if b"'" in code or b'"' in code:
        return None
    code = code.split(b"#", 1)[0].rstrip()
    if code.endswith(b"\\"):
        return None
    for ch in code:
        if ch in b"([{":
            depth += 1
        elif ch in b")]}":
            depth -= 1
            if depth < 0:
                return None
    return depth


def class_statements_to_parse(src, special_bases):
    """Class statements that may have a decorator, a keyword, type parameters, a base that is not
    a plain dotted name, or a base whose last name is in special_bases, found from the bytes
    alone (~30x cheaper than parsing; an undotted base bound by some `as NAME` in the module
    counts as possibly special): a list of (line, class name or None, at column 0, why);
    the name is given only when exactly one class line of the module has it. An empty list
    means the module certainly has none.

    Why this never misses one: a class statement's `class` keyword and each decorator's `@`
    are the first token of a logical line, hence of a physical line (a compound statement cannot
    follow `;`, and a backslash continuation would join them into the previous line). A class
    line that starts with `class Name(dotted, ...):` carries its whole header. A decorator's
    logical line ends where its brackets balance, once each physical line's complete strings and
    comment are removed (a string or backslash continuing past a line stops the scan); the
    decorated statement is the next line with code at the same indentation: another decorator,
    a def (fine) or a class (reported). Anything else is reported with name None. A false report
    (a string that looks like a class line or a decorator) only costs a parse.
    """
    lines = src.splitlines()  # \n, \r\n and \r: Python's newlines
    out = []
    names = {}
    # `from enum import Enum as E`: a base named by such an alias is judged by what it resolves to,
    # which only parsing tells (a dotted base keeps its last name through an alias of its head)
    aliases = {m.decode() for m in _AS_NAME.findall(src)}
    for i, line in enumerate(lines):
        stripped = line.lstrip(b" \t\f")
        if stripped.startswith(b"class") and not (stripped[5:6].isalnum() or stripped[5:6] == b"_"):
            name = _CLASS_LINE.match(stripped)
            if name:
                names[name.group(1).decode()] = names.get(name.group(1).decode(), 0) + 1
    for i, line in enumerate(lines):
        stripped = line.lstrip(b" \t\f")
        if stripped.startswith(b"class") and not (stripped[5:6].isalnum() or stripped[5:6] == b"_"):
            m = _SIMPLE_CLASS_HEADER.match(stripped)
            name = _CLASS_LINE.match(stripped)
            name = name.group(1).decode() if name else None
            top = len(stripped) == len(line)
            if m is None:
                out.append((i + 1, name, top, "class header of another shape"))
                continue
            for base in (m.group(1) or b"").split(b","):
                base = base.strip()
                if base and (base.rsplit(b".", 1)[-1].decode() in special_bases
                             or (b"." not in base and base.decode() in aliases)):
                    out.append((i + 1, name, top, f"base {base.decode()}"))
                    break
        elif stripped.startswith(b"@"):
            depth, j = _code_depth(stripped, 0), i
            while depth:  # a decorator spanning lines: follow it to its last line
                j += 1
                depth = _code_depth(lines[j], depth) if j < len(lines) else None
            if depth is None:
                out.append((i + 1, None, False, "decorator whose end was not found"))
                continue
            indent = line[: len(line) - len(stripped)]
            j += 1
            while j < len(lines) and not _has_code(lines[j]):
                j += 1  # blank and comment-only lines may sit between a decorator and its statement
            if j == len(lines):
                out.append((i + 1, None, False, "decorator without a statement"))
                continue
            nxt = lines[j]
            body = nxt.lstrip(b" \t\f")
            if nxt[: len(nxt) - len(body)] != indent:
                out.append((j + 1, None, False, "decorated statement at another indentation"))
            elif body.startswith(b"class") and not (body[5:6].isalnum() or body[5:6] == b"_"):
                name = _CLASS_LINE.match(body)
                out.append((j + 1, name.group(1).decode() if name else None, not indent, "decorated class"))
            elif not (body.startswith(b"@") or _DEF_START.match(body)):
                out.append((j + 1, None, False, "decorated statement is not a def"))
    # A name with several class statements in the module is reported without it: the maps
    # describe only the class the name ends up bound to.
    return [(line, name if names.get(name) == 1 else None, top, why) for line, name, top, why in out]


def _absolute_module(module, is_package, level, name):
    if not level:
        return name
    parts = module.split(".")
    if not is_package:
        parts = parts[:-1]
    if level - 1:
        if level - 1 > len(parts):
            return None
        parts = parts[: len(parts) - (level - 1)]
    if not parts:
        return None
    return ".".join(parts + ([name] if name else []))


def _module_bindings(tree, module, is_package):
    """name -> every binding of that name anywhere in the module, as ("module", dotted) for
    `import x.y [as z]`, ("from", dotted) for `from m import n [as z]`, or ("other",)."""
    out = {}

    def bind(name, what):
        out.setdefault(name, []).append(what)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    bind(a.asname, ("module", a.name))
                else:
                    top = a.name.split(".")[0]
                    bind(top, ("module", top))
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(module, is_package, node.level, node.module)
            for a in node.names:
                if a.name == "*":
                    bind("*", ("other",))
                else:
                    bind(a.asname or a.name, ("from", f"{base}.{a.name}") if base else ("other",))
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bind(node.id, ("other",))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name, ("other",))
        elif isinstance(node, ast.arg):
            bind(node.arg, ("other",))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bind(node.name, ("other",))
        elif isinstance(node, (getattr(ast, "MatchAs", ()), getattr(ast, "MatchStar", ()))) and node.name:
            bind(node.name, ("other",))
        elif isinstance(node, getattr(ast, "MatchMapping", ())) and node.rest:
            bind(node.rest, ("other",))
        elif type(node).__name__ in ("TypeVar", "ParamSpec", "TypeVarTuple", "TypeAlias"):
            name = getattr(node, "name", None)
            if isinstance(name, str):
                bind(name, ("other",))
    return out


def _resolver(bindings):
    import builtins

    def resolve(dotted):
        """The fully dotted name `dotted` (source text) refers to, or None when the module binds
        its first name more than once, by anything but an import, or may bind it by `import *`."""
        if dotted is None:
            return None
        head, _, rest = dotted.partition(".")
        found = bindings.get(head)
        if "*" in bindings:
            return None
        if found is None:
            return f"builtins.{dotted}" if hasattr(builtins, head) else None
        if len(found) != 1 or found[0][0] == "other":
            return None
        target = found[0][1]
        return f"{target}.{rest}" if rest else target

    return resolve


def module_class_facts(src, module, is_package=False):
    """ClassFacts for every class statement of a module's source (functions included), or None
    when it does not parse."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return None
    resolve = _resolver(_module_bindings(tree, module, is_package))
    out = []

    def facts(node, qualname):
        decorators = []
        for d in node.decorator_list:
            call = isinstance(d, ast.Call)
            target = d.func if call else d
            text = _dotted(target)
            decorators.append((text or "?", resolve(text), call))
        bases = tuple((_dotted(b), resolve(_dotted(b))) for b in node.bases)
        return ClassFacts(qualname, node.lineno, tuple(decorators), bases, bool(node.keywords),
                          bool(getattr(node, "type_params", None)))

    def walk(body, prefix, local):
        # A class statement only ever sits in a statement body: of the module, a class, a
        # function, a compound statement, an except handler or a match case.
        for node in body:
            if isinstance(node, ast.ClassDef):
                qual = None if local else (f"{prefix}.{node.name}" if prefix else node.name)
                out.append(facts(node, qual))
                walk(node.body, qual, local)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(node.body, None, True)
            else:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.stmt):
                        walk([child], prefix, local)
                    elif isinstance(child, (ast.excepthandler, getattr(ast, "match_case", ()))):
                        walk(child.body, prefix, local)

    try:
        walk(tree.body, None, False)
        if len(out) != sum(isinstance(n, ast.ClassDef) for n in ast.walk(tree)):
            return None  # a class statement this walk did not reach: unknown
    except RecursionError:
        return None
    return out


def _guarded_literal_expr(a, b, hashable):
    """_literal_expr, but every changed literal piece (old and new) must pass ast.literal_eval
    in the context it sits in: a dict key or set element (or an element of a tuple that is
    one) must also be hashable. Accepts a subset of what _literal_expr accepts.

    `hashable` is true when the value of a/b is used as a dict key or set element.
    """
    if dump(a) == dump(b):
        return True  # unchanged: the old module imported, so it built this before
    if _pure_literal(a) and _pure_literal(b):
        return _evaluates(a, hashable) and _evaluates(b, hashable)
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.Dict):
        if hashable:
            return False  # a dict is never hashable
        ka = [dump(k) for k in a.keys if k is not None]
        kb = [dump(k) for k in b.keys if k is not None]
        if len(set(ka)) != len(ka) or len(set(kb)) != len(kb):
            return False  # a repeated key hides the earlier entry's value from the matching below
        pa = {dump(k): (k, v) for k, v in zip(a.keys, a.values) if k is not None}
        pb = {dump(k): (k, v) for k, v in zip(b.keys, b.values) if k is not None}
        if None in a.keys or None in b.keys:
            if dump_list([v for k, v in zip(a.keys, a.values) if k is None]) != \
                    dump_list([v for k, v in zip(b.keys, b.values) if k is None]):
                return False
        for key in set(pa) | set(pb):
            ea, eb = pa.get(key), pb.get(key)
            if ea is None or eb is None:
                # an entry was added or removed: its key must be a hashable literal, its value a literal
                kn, v = ea if ea is not None else eb
                if not (_pure_literal(kn) and _pure_literal(v)):
                    return False
                if not (_evaluates(kn, hashable=True) and _evaluates(v)):
                    return False
            elif not _guarded_literal_expr(ea[1], eb[1], False):
                return False
        return True
    if isinstance(a, (ast.List, ast.Tuple, ast.Set)):
        if hashable and not isinstance(a, ast.Tuple):
            return False  # lists and sets are never hashable
        # set elements must hash; tuple elements must hash when the tuple itself must
        inner = isinstance(a, ast.Set) or hashable
        if len(a.elts) == len(b.elts):
            return all(_guarded_literal_expr(x, y, inner) for x, y in zip(a.elts, b.elts))
        da, db = [dump(x) for x in a.elts], [dump(y) for y in b.elts]
        sm = SequenceMatcher(None, da, db, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            for x in a.elts[i1:i2] + b.elts[j1:j2]:
                if not (_pure_literal(x) and _evaluates(x, inner)):
                    return False
        return True
    # A UnaryOp pair reaches here only when an operand is not a pure literal. `not` is
    # rejected by rule; unary -/+ on anything but a number raises, so neither can narrow.
    return False


def _literal_expr(a, b):
    if dump(a) == dump(b):
        return True
    if _pure_literal(a) and _pure_literal(b):
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.Dict):
        pa = {dump(k): v for k, v in zip(a.keys, a.values) if k is not None}
        pb = {dump(k): v for k, v in zip(b.keys, b.values) if k is not None}
        if None in a.keys or None in b.keys:
            if dump_list([v for k, v in zip(a.keys, a.values) if k is None]) != \
                    dump_list([v for k, v in zip(b.keys, b.values) if k is None]):
                return False
        for key in set(pa) | set(pb):
            va, vb = pa.get(key), pb.get(key)
            if va is None or vb is None:
                # an entry was added or removed: fine when its key and value are literals
                v = va if va is not None else vb
                kn = next(k for k in (a.keys if va is not None else b.keys) if k is not None and dump(k) == key)
                if not (_pure_literal(kn) and _pure_literal(v)):
                    return False
            elif not _literal_expr(va, vb):
                return False
        return True
    if isinstance(a, (ast.List, ast.Tuple, ast.Set)):
        if len(a.elts) == len(b.elts):
            return all(_literal_expr(x, y) for x, y in zip(a.elts, b.elts))
        da, db = [dump(x) for x in a.elts], [dump(y) for y in b.elts]
        sm = SequenceMatcher(None, da, db, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if not all(_pure_literal(x) for x in a.elts[i1:i2] + b.elts[j1:j2]):
                return False
        return True
    if isinstance(a, ast.UnaryOp):
        return type(a.op) is type(b.op) and _literal_expr(a.operand, b.operand)
    return False


def dump_field(node, field):
    value = getattr(node, field, None)
    if isinstance(value, list):
        return dump_list(value)
    if isinstance(value, ast.AST):
        return dump(value)
    return repr(value)


def dump_list(values):
    return "[" + ", ".join(dump(v) if isinstance(v, ast.AST) else repr(v) for v in values) + "]"


def _strip_annotations(args):
    """The parts of a signature that matter at run time when annotations are never evaluated."""
    out = []
    for a in args.posonlyargs + args.args + args.kwonlyargs + [x for x in (args.vararg, args.kwarg) if x]:
        out.append(a.arg)
    out.append(dump_list(args.defaults))
    out.append(dump_list([d for d in args.kw_defaults]) if args.kw_defaults else "")
    out.append(len(args.posonlyargs))
    out.append(len(args.kwonlyargs))
    out.append(bool(args.vararg))
    out.append(bool(args.kwarg))
    return out


def _only_constant_defaults(a, b):
    """True when the two signatures differ only in constant default values."""
    if _strip_annotations(a)[:-6] != _strip_annotations(b)[:-6]:
        return False
    da = a.defaults + [d for d in a.kw_defaults if d is not None]
    db = b.defaults + [d for d in b.kw_defaults if d is not None]
    if len(da) != len(db):
        return False
    for x, y in zip(da, db):
        if dump(x) != dump(y) and not (_is_constant_tree(x) and _is_constant_tree(y)):
            return False
    return True


def _is_constant_tree(node):
    return all(isinstance(n, (ast.Constant, ast.UnaryOp, ast.USub, ast.UAdd, ast.Not, ast.Tuple, ast.Load))
               for n in ast.walk(node))


_COMPOUND = {
    ast.If: (("test",), ("body", "orelse")),
    ast.While: (("test",), ("body", "orelse")),
    ast.For: (("target", "iter", "type_comment"), ("body", "orelse")),
    ast.AsyncFor: (("target", "iter", "type_comment"), ("body", "orelse")),
    ast.With: (("items", "type_comment"), ("body",)),
    ast.AsyncWith: (("items", "type_comment"), ("body",)),
    ast.Try: ((), ("body", "handlers", "orelse", "finalbody")),
    ast.Match: (("subject",), ("cases",)),
}
if hasattr(ast, "TryStar"):
    _COMPOUND[ast.TryStar] = ((), ("body", "handlers", "orelse", "finalbody"))


def _diff_handlers_impl(self, ob, nb, scope, owner):
    if len(ob) != len(nb):
        self._site_for_scope(scope, span(owner), {"*"}, "handlers changed")
        return
    for o, n in zip(ob, nb):
        if dump_field(o, "type") != dump_field(n, "type") or o.name != n.name:
            hdr = set(range(o.lineno, (o.type.end_lineno if o.type else o.lineno) + 1))
            self._site_for_scope(scope, hdr | entry_lines(owner), {"*"}, "except header")
        self.diff_body(o.body, n.body, scope, set(range(o.lineno, o.lineno + 1)))


def _diff_cases_impl(self, ob, nb, scope, owner):
    if len(ob) != len(nb):
        self._site_for_scope(scope, span(owner), {"*"}, "match cases changed")
        return
    for o, n in zip(ob, nb):
        if dump_field(o, "pattern") != dump_field(n, "pattern") or dump_field(o, "guard") != dump_field(n, "guard"):
            self._site_for_scope(scope, entry_lines(owner) | set(range(o.pattern.lineno, o.pattern.end_lineno + 1)),
                                 {"*"}, "case header")
        self.diff_body(o.body, n.body, scope, set(range(o.pattern.lineno, o.pattern.lineno + 1)))


Analysis._diff_handlers = _diff_handlers_impl
Analysis._diff_cases = _diff_cases_impl


def _diff_points(a, b, owner, out):
    """Collect (owner, node) for every place a and b differ.

    owner is the innermost Lambda or GeneratorExp whose *call-time* part contains
    the difference, or None when the difference runs where the expression is built.
    """
    if type(a) is not type(b):
        out.append((owner, a))
        return
    if isinstance(a, ast.AST):
        if isinstance(a, ast.Lambda):
            if dump(a.args) != dump(b.args):
                out.append((None, a))
            _diff_points(a.body, b.body, a, out)
            return
        if isinstance(a, ast.GeneratorExp):
            if len(a.generators) != len(b.generators):
                out.append((None, a))
                return
            _diff_points(a.generators[0].iter, b.generators[0].iter, owner, out)
            _diff_points(a.elt, b.elt, a, out)
            for i, (ga, gb) in enumerate(zip(a.generators, b.generators)):
                if i:
                    _diff_points(ga.iter, gb.iter, a, out)
                _diff_points(ga.target, gb.target, a, out)
                if dump_list(ga.ifs) != dump_list(gb.ifs):
                    out.append((a, ga))
                if ga.is_async != gb.is_async:
                    out.append((None, a))
            return
        for field in a._fields:
            va, vb = getattr(a, field, None), getattr(b, field, None)
            if isinstance(va, list):
                if not isinstance(vb, list) or len(va) != len(vb):
                    out.append((owner, a))
                    continue
                for x, y in zip(va, vb):
                    _diff_points(x, y, owner, out)
            elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
                if va is None or vb is None:
                    out.append((owner, a))
                else:
                    _diff_points(va, vb, owner, out)
            elif va != vb or type(va) is not type(vb):
                out.append((owner, a))
    elif a != b:
        out.append((owner, a))


_old_trees = {}


def analyse(old_src, new_src, key=None):
    """Change sites between two versions; `key` caches the parsed old version (same pinned file)."""
    old_tree = None
    if key is not None:
        hit = _old_trees.get(key)
        if hit is not None and hit[0] == old_src:
            old_tree = hit[1]
        else:
            try:
                old_tree = ast.parse(old_src)
                _old_trees[key] = (old_src, old_tree)
            except (SyntaxError, ValueError):
                old_tree = None
    _dump_cache.clear()
    module, is_package = None, False
    if isinstance(key, str) and key.endswith(".py"):
        module = key[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module, is_package = module[: -len(".__init__")], True
    sites = Analysis(old_src, new_src, module=module, is_package=is_package).run(old_tree)
    _dump_cache.clear()
    return sites
