#!/usr/bin/env python3
"""Static facts about the pinned sources: where each name is read, and dynamic attribute reads.

    python3 tools/static_index.py --repo /src/sqlglot --out work/static.json

For every .py file under sqlglot/ and tests/:
  loads:   name -> [[file, line, scope, base, owner]]
           scope "call" (inside a function/lambda) or "import" (module/class body),
           base  the expression the attribute is read from ("self", "cls", "exp.Foo",
                 "?" when not a plain name chain, "" for a bare name, '"' for a string constant),
           owner enclosing class qualname for import-scope reads, else the enclosing function name.
  dynamic: [[file, line, regex or null]] attribute reads whose name is computed;
           null means any name could be read.
"""

import argparse
import ast
import json
import os
import re
import sys

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DYN_FUNCS = {"getattr", "hasattr", "setattr", "delattr"}


def chain(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return "?"


def pattern_from(node, guards):
    """Regex for the attribute names a computed name expression can produce, or None for any."""
    if isinstance(node, ast.JoinedStr):
        out = ""
        for v in node.values:
            if isinstance(v, ast.Constant):
                out += re.escape(str(v.value))
            else:
                out += ".*"
        return "^" + out + "$"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = pattern_from(node.left, guards)
        right = pattern_from(node.right, guards)
        lp = left[1:-1] if left else ".*"
        rp = right[1:-1] if right else ".*"
        return "^" + lp + rp + "$"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return "^" + re.escape(node.value) + "$"
    if isinstance(node, ast.Name) and node.id in guards:
        return guards[node.id]
    return None


def endswith_guards(func):
    """Loop variables filtered by `if not v.endswith("x") ...: continue` style guards."""
    guards = {}
    for node in ast.walk(func):
        if not isinstance(node, (ast.For, ast.comprehension)):
            continue
        target = node.target
        if not isinstance(target, ast.Name):
            continue
        v = target.id

        def endswith_call(e):
            if (isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr == "endswith"
                    and isinstance(e.func.value, ast.Name) and e.func.value.id == v and len(e.args) == 1
                    and isinstance(e.args[0], ast.Constant) and isinstance(e.args[0].value, str)):
                return e.args[0].value
            return None

        def required_suffix(test, negated):
            """Suffix every name must have to get past `test` (keep-if or skip-if-not)."""
            if negated:
                # `if not v.endswith(S) [or ...]: continue`
                parts = test.values if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or) else [test]
                for p in parts:
                    if isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not):
                        s = endswith_call(p.operand)
                        if s is not None:
                            return s
                return None
            parts = test.values if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) else [test]
            for p in parts:
                s = endswith_call(p)
                if s is not None:
                    return s
            return None

        suffix = None
        if isinstance(node, ast.For):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.If) and first.body and isinstance(first.body[0], ast.Continue):
                suffix = required_suffix(first.test, negated=True)
            if suffix is None and len(node.body) == 1 and isinstance(first, ast.If) and not first.orelse:
                suffix = required_suffix(first.test, negated=False)
        else:
            for t in node.ifs:
                suffix = suffix or required_suffix(t, negated=False)
        if suffix is not None:
            guards[v] = "^.*" + re.escape(suffix) + "$"
    return guards


def _is_module_expr(node):
    """importlib.import_module(...), __import__(...), sys.modules[...]"""
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        return name in ("import_module", "__import__")
    if isinstance(node, ast.Subscript):
        v = node.value
        return isinstance(v, ast.Attribute) and v.attr == "modules" and isinstance(v.value, ast.Name) \
            and v.value.id == "sys"
    return False


def _module_vars(func):
    """Local names that are only ever assigned a module object inside this function."""
    assigned = {}
    for n in ast.walk(func):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    assigned.setdefault(t.id, []).append(_is_module_expr(n.value))
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
            t = n.target
            if isinstance(t, ast.Name):
                assigned.setdefault(t.id, []).append(False)
        elif isinstance(n, (ast.For, ast.comprehension, ast.With, ast.withitem)):
            tgt = getattr(n, "target", None) or getattr(n, "optional_vars", None)
            for x in ast.walk(tgt) if tgt is not None else ():
                if isinstance(x, ast.Name):
                    assigned.setdefault(x.id, []).append(False)
    params = {a.arg for a in func.args.args + func.args.posonlyargs + func.args.kwonlyargs}
    return {k for k, v in assigned.items() if v and all(v) and k not in params}


class Indexer(ast.NodeVisitor):
    def __init__(self, path, loads, dynamic, class_bound):
        self.path = path
        self.class_bound = class_bound
        self.loads = loads
        self.dynamic = dynamic
        self.func_stack = []
        self.class_stack = []
        self.guards = [{}]
        self.module_vars = [set()]

    def _class_key(self):
        module = self.path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        return f"{module}:{'.'.join(self.class_stack)}" if self.class_stack else ""

    def _base_kind(self, base):
        """'module' when the object is plainly a module, 'self' for the receiver, else 'other'."""
        if _is_module_expr(base):
            return "module"
        if isinstance(base, ast.Name):
            if base.id in ("self", "cls") and self.func_stack:
                return "self"
            if base.id in self.module_vars[-1]:
                return "module"
        return "other"

    def _scope(self):
        return "call" if self.func_stack else "import"

    def _owner(self):
        if self.func_stack:
            return self.func_stack[-1]
        return ".".join(self.class_stack)

    def _add(self, name, line, base):
        self.loads.setdefault(name, []).append([self.path, line, self._scope(), base, self._owner()])

    def visit_FunctionDef(self, node):
        for d in node.decorator_list:
            self.visit(d)
        self.visit(node.args)
        if node.returns:
            self.visit(node.returns)
        if self.class_stack and not self.func_stack:
            self.class_bound.add(node.name)
        self.func_stack.append(node.name)
        self.guards.append({**self.guards[-1], **endswith_guards(node)})
        self.module_vars.append(_module_vars(node))
        for s in node.body:
            self.visit(s)
        self.module_vars.pop()
        self.guards.pop()
        self.func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.visit(node.args)
        self.func_stack.append("<lambda>")
        self.visit(node.body)
        self.func_stack.pop()

    def visit_ClassDef(self, node):
        for d in node.decorator_list + node.bases + [k.value for k in node.keywords]:
            self.visit(d)
        saved = self.func_stack
        if self.func_stack:
            # a class body inside a function runs when the function runs
            pass
        if self.class_stack and not self.func_stack:
            self.class_bound.add(node.name)
        self.class_stack.append(node.name)
        self.func_stack = [] if not saved else saved
        for s in node.body:
            self.visit(s)
        self.func_stack = saved
        self.class_stack.pop()

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self._add(node.id, node.lineno, "")
        elif self.class_stack and not self.func_stack:
            self.class_bound.add(node.id)

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            self._add(node.attr, node.lineno, chain(node.value))
            if node.attr == "__dict__" and id(node) not in self.const_dict_reads:
                self.dynamic.append([self.path, node.lineno, None, self._base_kind(node.value), self._class_key()])
        self.visit(node.value)

    def _mark_const_dict_reads(self, tree):
        """X.__dict__.get("K"), X.__dict__["K"] and "K" in X.__dict__ read one known name."""
        self.const_dict_reads = set()
        for n in ast.walk(tree):
            target = None
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                    and n.args and isinstance(n.args[0], ast.Constant)):
                target = n.func.value
            elif isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant):
                target = n.value
            elif (isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], (ast.In, ast.NotIn))
                  and isinstance(n.left, ast.Constant)):
                target = n.comparators[0]
            if isinstance(target, ast.Attribute) and target.attr == "__dict__":
                self.const_dict_reads.add(id(target))

    def visit_Constant(self, node):
        if isinstance(node.value, str) and len(node.value) <= 80 and IDENT.match(node.value):
            self._add(node.value, node.lineno, '"')

    def visit_Call(self, node):
        f = node.func
        fname = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
        if fname in DYN_FUNCS and len(node.args) >= 2:
            name_arg = node.args[1]
            if not (isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str)):
                self.dynamic.append([self.path, node.lineno, pattern_from(name_arg, self.guards[-1]),
                                     self._base_kind(node.args[0]), self._class_key()])
        elif fname in ("vars", "getmembers", "getattr_static", "attrgetter", "__getattribute__"):
            base = node.args[0] if node.args else None
            self.dynamic.append([self.path, node.lineno, None,
                                 self._base_kind(base) if base is not None else "other", self._class_key()])
        self.generic_visit(node)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    loads, dynamic, class_bound = {}, [], set()
    n = 0
    for top in ("sqlglot", "tests"):
        for root, _, files in os.walk(os.path.join(args.repo, top)):
            for f in sorted(files):
                if not f.endswith(".py"):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, args.repo)
                try:
                    tree = ast.parse(open(full, "rb").read())
                except SyntaxError:
                    continue
                ix = Indexer(rel, loads, dynamic, class_bound)
                ix._mark_const_dict_reads(tree)
                ix.visit(tree)
                n += 1
    with open(args.out, "w") as fh:
        json.dump({"loads": loads, "dynamic": dynamic, "class_bound": sorted(class_bound)}, fh)
    wild = sum(1 for d in dynamic if d[2] is None)
    print(f"{n} files, {len(loads)} names, {sum(map(len, loads.values()))} reads, "
          f"{len(dynamic)} dynamic sites ({wild} unrestricted)", file=sys.stderr)


if __name__ == "__main__":
    main()
