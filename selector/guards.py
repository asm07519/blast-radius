"""Static AST guards for cross-test side effects and endless loops.

  BR_MUTATION_GUARD=1  a patch that adds a function-body mutation of shared class-level or
                       module-level state selects every test (mutated state persists across
                       tests in the same pytest process, so tests that never execute the
                       changed line can fail).
  BR_HANG_GUARD=1      a patch that adds an obviously endless loop (no break/return/raise/yield
                       exit) selects every test.
"""

import ast
from collections import Counter

_MUTATING_METHODS = frozenset({
    "pop", "popitem", "update", "clear", "setdefault",
    "append", "extend", "insert", "remove", "add", "discard",
    "sort", "reverse", "difference_update", "intersection_update", "symmetric_difference_update",
})

# Lowercase mutable class attributes inherited from base classes in other modules of sqlglot.
_KNOWN_CLASS_ATTRS = frozenset({"arg_types", "classes", "_classes"})


def _collect_scope_bindings(fn):
    """(locals_set, globals_set) for a function body (excluding nested defs/classes)."""
    locs = set()
    globs = set()
    for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
        locs.add(a.arg)
    if fn.args.vararg:
        locs.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        locs.add(fn.args.kwarg.arg)

    def walk(node):
        if node is not fn and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            locs.add(node.name)
            return
        if isinstance(node, ast.Global):
            globs.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            locs.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            locs.add(node.name)
        for ch in ast.iter_child_nodes(node):
            walk(ch)

    for s in fn.body:
        walk(s)
    locs -= globs
    return locs, globs


def _module_symbols(tree):
    """(module_globals, class_names, class_body_attrs) defined or imported at module scope."""
    globs = set()
    classes = set()
    class_attrs = set(_KNOWN_CLASS_ATTRS)

    def collect_class(cls_node):
        classes.add(cls_node.name)
        for cs in cls_node.body:
            if isinstance(cs, ast.ClassDef):
                collect_class(cs)
            elif isinstance(cs, ast.Assign):
                for t in cs.targets:
                    if isinstance(t, ast.Name):
                        class_attrs.add(t.id)
            elif isinstance(cs, ast.AnnAssign) and isinstance(cs.target, ast.Name):
                class_attrs.add(cs.target.id)

    for s in tree.body:
        if isinstance(s, ast.ClassDef):
            collect_class(s)
        elif isinstance(s, ast.Assign):
            for t in s.targets:
                if isinstance(t, ast.Name):
                    globs.add(t.id)
        elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
            globs.add(s.target.id)
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            for a in s.names:
                globs.add((a.asname or a.name).split(".")[0])
    return globs, classes, class_attrs


def _is_shared_target(node, locs, declared_globs, mod_globs, classes, class_attrs, attr_write=False):
    """True when `node` refers to a module global or class-level attribute (not a local or
    plain instance attribute)."""
    if isinstance(node, ast.Name):
        if node.id in declared_globs:
            return True
        if node.id in locs:
            return False
        return node.id in classes or node.id in mod_globs or node.id.isupper()
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            if attr_write:
                return False  # `self.x = y` binds on the instance, not the class
            return node.attr in class_attrs or node.attr.isupper()
        if isinstance(node.value, ast.Name) and node.value.id == "cls":
            return True
        if isinstance(node.value, ast.Name) and node.value.id not in locs:
            if node.value.id in classes or node.value.id in mod_globs or node.value.id in declared_globs:
                return True
        if isinstance(node.value, ast.Attribute) and node.value.attr == "__class__":
            return True
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "type":
            return True
        return _is_shared_target(node.value, locs, declared_globs, mod_globs, classes, class_attrs, attr_write=False)
    if isinstance(node, ast.Subscript):
        return _is_shared_target(node.value, locs, declared_globs, mod_globs, classes, class_attrs, attr_write=False)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "globals" and not node.args:
        return True
    return False


def _shared_mutations(src):
    """Counter of AST dumps of function-body mutations of shared state, plus sample labels."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return Counter(), {}
    mod_globs, classes, class_attrs = _module_symbols(tree)
    counts = Counter()
    labels = {}

    def record(node, label):
        key = ast.dump(node, include_attributes=False)
        counts[key] += 1
        labels.setdefault(key, label)

    def walk_fn(fn, outer_locs):
        fn_locs, declared_globs = _collect_scope_bindings(fn)
        locs = (outer_locs - declared_globs) | fn_locs

        def scan(node):
            if node is not fn and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk_fn(node, locs)
                return
            if isinstance(node, ast.ClassDef):
                for s in node.body:
                    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        walk_fn(s, locs)
                return
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATING_METHODS:
                    if _is_shared_target(node.func.value, locs, declared_globs, mod_globs, classes, class_attrs):
                        record(node, f".{node.func.attr}()")
                elif isinstance(node.func, ast.Name) and node.func.id in ("setattr", "delattr") and node.args:
                    if _is_shared_target(node.args[0], locs, declared_globs, mod_globs, classes, class_attrs):
                        record(node, f"{node.func.id}()")
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in declared_globs:
                        record(t, f"global {t.id} = ...")
                    elif isinstance(t, ast.Subscript) and _is_shared_target(
                            t.value, locs, declared_globs, mod_globs, classes, class_attrs):
                        record(t, "shared subscript assign")
                    elif isinstance(t, ast.Attribute) and _is_shared_target(
                            t, locs, declared_globs, mod_globs, classes, class_attrs, attr_write=True):
                        record(t, f".{t.attr} = ...")
            elif isinstance(node, ast.AugAssign):
                t = node.target
                if isinstance(t, ast.Name) and t.id in declared_globs:
                    record(t, f"global {t.id} op= ...")
                elif isinstance(t, ast.Subscript) and _is_shared_target(
                        t.value, locs, declared_globs, mod_globs, classes, class_attrs):
                    record(t, "shared subscript augassign")
                elif isinstance(t, ast.Attribute) and _is_shared_target(
                        t, locs, declared_globs, mod_globs, classes, class_attrs, attr_write=False):
                    record(t, f".{t.attr} op= ...")
            elif isinstance(node, ast.Delete):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in declared_globs:
                        record(t, f"del global {t.id}")
                    elif isinstance(t, ast.Subscript) and _is_shared_target(
                            t.value, locs, declared_globs, mod_globs, classes, class_attrs):
                        record(t, "del shared subscript")
                    elif isinstance(t, ast.Attribute) and _is_shared_target(
                            t, locs, declared_globs, mod_globs, classes, class_attrs, attr_write=True):
                        record(t, f"del .{t.attr}")
            for ch in ast.iter_child_nodes(node):
                scan(ch)

        for s in fn.body:
            scan(s)

    def walk_top(body):
        for s in body:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk_fn(s, set())
            elif isinstance(s, ast.ClassDef):
                walk_top(s.body)

    walk_top(tree.body)
    return counts, labels


def added_shared_mutation(old_src, new_src):
    """Short description if `new_src` adds a function-body mutation of shared class/module state
    not present in `old_src`, else None."""
    new_counts, new_labels = _shared_mutations(new_src)
    if not new_counts:
        return None
    old_counts, _ = _shared_mutations(old_src)
    diff = new_counts - old_counts
    if not diff:
        return None
    key = next(iter(diff))
    return new_labels.get(key, "shared state mutation")


def _loop_has_exit(loop_node):
    """True if `loop_node.body` contains a break targeting this loop, or a return/raise/yield
    in the same function scope."""
    def has_yield(expr):
        for n in ast.walk(expr):
            if isinstance(n, (ast.Yield, ast.YieldFrom)):
                return True
        return False

    def walk(stmts, in_nested_loop):
        for s in stmts:
            if isinstance(s, (ast.Return, ast.Raise)):
                return True
            if isinstance(s, ast.Break) and not in_nested_loop:
                return True
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(s, (ast.For, ast.AsyncFor, ast.While)):
                if walk(s.body, True) or walk(s.orelse, in_nested_loop):
                    return True
                continue
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(s, field, None)
                if isinstance(sub, list) and walk(sub, in_nested_loop):
                    return True
            if isinstance(s, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                for h in s.handlers:
                    if walk(h.body, in_nested_loop):
                        return True
            if hasattr(ast, "Match") and isinstance(s, ast.Match):
                for c in s.cases:
                    if walk(c.body, in_nested_loop):
                        return True
            if has_yield(s):
                return True
        return False

    return walk(loop_node.body, False)


def _is_truthy_const(node):
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return isinstance(node.operand, ast.Constant) and not bool(node.operand.value)
    return False


def _endless_loop_kind(node):
    if isinstance(node, ast.While):
        if _is_truthy_const(node.test) and not _loop_has_exit(node):
            return "while True"
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        it = node.iter
        if isinstance(it, ast.Call):
            fn = it.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
            if name == "iter" and len(it.args) == 2 and not _loop_has_exit(node):
                return "for ... in iter(..., ...)"
            if (name in ("count", "cycle") or (name == "repeat" and len(it.args) == 1 and not it.keywords)) \
                    and not _loop_has_exit(node):
                return f"for ... in {name}(...)"
    return None


def _endless_loops(src):
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return Counter()
    counts = Counter()
    for n in ast.walk(tree):
        kind = _endless_loop_kind(n)
        if kind is not None:
            counts[kind] += 1
    return counts


def added_endless_loop(old_src, new_src):
    """Short description if `new_src` adds an exit-free endless loop not present in `old_src`,
    else None."""
    new_counts = _endless_loops(new_src)
    if not new_counts:
        return None
    old_counts = _endless_loops(old_src)
    diff = new_counts - old_counts
    if not diff:
        return None
    return next(iter(diff))
