#!/usr/bin/env python3
"""Static AST classifier of name/attribute load sites for BR_CELLS soundness.

    python3 tools/table_sites.py --repo /src/sqlglot --out work/table_sites.json

C code can read a DictProxy/SetProxy silently (e.g. set(p), a.update(p), a.issubset(p)).
So a test counts as a pure keyed reader of a table name only if EVERY load site of that
name it executed is statically proven "keyed-only":
  * subscripted directly: `X[k]`
  * `.get(k, ...)` called directly on it: `X.get(k)`
  * right operand of `in` / `not in`: `k in X` / `k not in X`

Every other load site of that name (passing it to a function, iterating over it, `len(X)`,
assigning `d = X`, etc.) is classified as "whole". Import-scope loads inside `sqlglot/` are
excluded (state_probe covers import-time derivation); import-scope loads inside `tests/` are
kept because they belong to that test file's tests.
"""

import argparse
import ast
import json
import os


def _is_keyed_load(node, parent):
    """True when `node` (Name or Attribute with Load ctx) is consumed solely by a keyed op."""
    if isinstance(parent, ast.Subscript) and parent.value is node and isinstance(parent.ctx, ast.Load):
        return True
    if isinstance(parent, ast.Compare) and len(parent.ops) == 1 and isinstance(parent.ops[0], (ast.In, ast.NotIn)):
        if len(parent.comparators) == 1 and parent.comparators[0] is node:
            return True
    if isinstance(parent, ast.Attribute) and parent.value is node and parent.attr == "get":
        # Checked by caller: parent itself must be the func of a Call (not `f = X.get`).
        return "get_attr"
    return False


def scan_file(path, rel_path):
    """Return {name: [[line, scope, is_whole], ...]} for a Python file."""
    with open(path, "rb") as fh:
        src = fh.read()
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return {}
    is_sqlglot = rel_path.startswith("sqlglot/")
    out = {}

    def walk(node, parent, grandparent, in_fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Defaults and decorators execute in the enclosing scope; body executes in call scope.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    walk(d, node, parent, in_fn)
                if node.returns is not None:
                    walk(node.returns, node, parent, in_fn)
            for d in list(node.args.defaults) + [x for x in node.args.kw_defaults if x is not None]:
                walk(d, node.args, node, in_fn)
            if isinstance(node, ast.Lambda):
                walk(node.body, node, parent, True)
            else:
                for s in node.body:
                    walk(s, node, parent, True)
            return

        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if in_fn or not is_sqlglot:
                k = _is_keyed_load(node, parent)
                if k == "get_attr":
                    keyed = isinstance(grandparent, ast.Call) and grandparent.func is parent
                else:
                    keyed = bool(k)
                if not keyed:
                    out.setdefault(node.id, []).append((node.lineno, "call" if in_fn else "import"))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            if in_fn or not is_sqlglot:
                k = _is_keyed_load(node, parent)
                if k == "get_attr":
                    keyed = isinstance(grandparent, ast.Call) and grandparent.func is parent
                else:
                    keyed = bool(k)
                if not keyed:
                    out.setdefault(node.attr, []).append((node.lineno, "call" if in_fn else "import"))

        for ch in ast.iter_child_nodes(node):
            walk(ch, node, parent, in_fn)

    walk(tree, None, None, False)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)
    whole_sites = {}  # name -> [[rel_path, line, scope], ...]
    for top in ("sqlglot", "tests"):
        root = os.path.join(repo, top)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for fn in sorted(filenames):
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, repo).replace(os.sep, "/")
                per = scan_file(full, rel)
                for name, hits in per.items():
                    dst = whole_sites.setdefault(name, [])
                    for line, scope in sorted(set(hits)):
                        dst.append([rel, line, scope])
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump({"whole_sites": whole_sites}, fh)


if __name__ == "__main__":
    main()
