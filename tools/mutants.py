#!/usr/bin/env python3
"""Generate test patches against the pinned sqlglot, of the kinds seen in the sample and others.

    python3 tools/mutants.py --repo /src/sqlglot --out work/mutants --count 200 --seed 1

Used only to check the selector for misses; nothing from these runs is shipped.
"""

import argparse
import ast
import difflib
import io
import os
import random
import tokenize

OPS = {
    "==": ["!="], "!=": ["=="], "<": ["<=", ">"], "<=": ["<"], ">": [">=", "<"], ">=": [">"],
    "+": ["-"], "-": ["+"], "*": ["+"], "//": ["/"], "%": ["//"], "|": ["&"], "&": ["|"],
    "+=": ["-="], "-=": ["+="],
}
NAMES = {"and": ["or"], "or": ["and"], "True": ["False"], "False": ["True"], "is": ["=="],
         "break": ["continue"], "continue": ["break"], "None": ["0", "''"]}
COMMENTS = ["# Cheap check first", "# Keep in sync with the generator", "# Intentionally not cached",
            "# Kept for backwards compatibility", "# TODO: revisit", "# noqa", "# type: ignore",
            "# dialect: snowflake", "# fmt: off", "# -*- coding: latin-1 -*-"]


def rel_files(repo, prefix, exts):
    out = []
    for root, _, files in os.walk(os.path.join(repo, prefix)):
        if "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(exts):
                out.append(os.path.relpath(os.path.join(root, f), repo))
    return sorted(out)


def read(repo, path):
    with open(os.path.join(repo, path), encoding="utf-8") as fh:
        return fh.read()


def diff(path, old, new, old_path=None, new_path=None):
    old_path = old_path or path
    new_path = new_path or path
    head = f"diff --git a/{old_path} b/{new_path}\n"
    if old is None:
        head += "new file mode 100644\n"
        a, b = [], new.splitlines(keepends=True)
        fromf, tof = "/dev/null", f"b/{new_path}"
    elif new is None:
        head += "deleted file mode 100644\n"
        a, b = old.splitlines(keepends=True), []
        fromf, tof = f"a/{old_path}", "/dev/null"
    else:
        a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
        fromf, tof = f"a/{old_path}", f"b/{new_path}"
    body = "".join(_fix(l) for l in difflib.unified_diff(a, b, fromf, tof, n=3))
    if not body and old_path != new_path:
        return head + f"similarity index 100%\nrename from {old_path}\nrename to {new_path}\n"
    return head + body


def _fix(line):
    return line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"


def ast_differs(old, new):
    try:
        return ast.dump(ast.parse(old)) != ast.dump(ast.parse(new))
    except SyntaxError:
        return True


def token_mutation(rng, src):
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    lines = src.splitlines(keepends=True)
    cands = []
    for i, t in enumerate(toks):
        if t.type == tokenize.OP and t.string in OPS:
            cands.append((t, OPS[t.string]))
        elif t.type == tokenize.NAME and t.string in NAMES:
            cands.append((t, NAMES[t.string]))
        elif t.type == tokenize.NUMBER and t.string.isdigit():
            n = int(t.string)
            cands.append((t, [str(n + 1)] + ([str(n - 1)] if n > 0 else [])))
        elif t.type == tokenize.NAME and t.string == "not" and i + 1 < len(toks) and toks[i + 1].type == tokenize.NAME:
            cands.append((t, [""]))
        elif t.type == tokenize.STRING and len(t.string) < 30 and t.string[-1] in "'\"" \
                and not t.string.startswith(("f", "rb", "b", "\"\"\"", "'''", "r")) and t.start[0] == t.end[0]:
            cands.append((t, [t.string[:-1] + "x" + t.string[-1]]))
    rng.shuffle(cands)
    for t, repl in cands[:40]:
        (sl, sc), (el, ec) = t.start, t.end
        if sl != el:
            continue
        r = rng.choice(repl)
        line = lines[sl - 1]
        if r == "":
            new_line = line[:sc] + line[ec:].lstrip(" ")
        else:
            new_line = line[:sc] + r + line[ec:]
        new = "".join(lines[:sl - 1] + [new_line] + lines[sl:])
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        if ast_differs(src, new):
            return new
    return None


def class_literal_mutation(rng, src):
    """Flip a literal inside a class-body assignment (settings, arg_types, ...)."""
    tree = ast.parse(src)
    cands = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for stmt in cls.body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                for n in ast.walk(stmt.value):
                    if isinstance(n, ast.Constant) and n.lineno == n.end_lineno and \
                            isinstance(n.value, (bool, int, str)) and not isinstance(n.value, bytes):
                        cands.append(n)
    rng.shuffle(cands)
    lines = src.splitlines(keepends=True)
    for n in cands[:30]:
        v = n.value
        if isinstance(v, bool):
            r = repr(not v)
        elif isinstance(v, int):
            r = str(v + 1)
        elif isinstance(v, str) and len(v) < 40 and "\n" not in v:
            r = repr(v + "x")
        else:
            continue
        line = lines[n.lineno - 1]
        enc = line.encode("utf-8")
        new_line = (enc[: n.col_offset] + r.encode() + enc[n.end_col_offset:]).decode("utf-8")
        new = "".join(lines[: n.lineno - 1] + [new_line] + lines[n.lineno:])
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        if ast_differs(src, new):
            return new
    return None


def cmp_swap_mutation(rng, src):
    """Swap is <-> == or is not <-> != at one comparison inside a function."""
    tree = ast.parse(src)
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    cands = []
    for f in funcs:
        for n in ast.walk(f):
            if isinstance(n, ast.Compare) and len(n.ops) == 1 and \
                    isinstance(n.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)) and \
                    n.left.end_lineno == n.comparators[0].lineno:
                cands.append(n)
    rng.shuffle(cands)
    lines = src.splitlines(keepends=True)
    swap = {"is not": "!=", "is": "==", "==": "is", "!=": "is not"}
    for n in cands[:20]:
        line = lines[n.left.end_lineno - 1]
        enc = line.encode("utf-8")
        a, b = n.left.end_col_offset, n.comparators[0].col_offset
        mid = enc[a:b].decode("utf-8")
        op = mid.strip()
        if op not in swap:
            continue
        new_line = (enc[:a] + mid.replace(op, swap[op], 1).encode() + enc[b:]).decode("utf-8")
        new = "".join(lines[: n.left.end_lineno - 1] + [new_line] + lines[n.left.end_lineno:])
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        if ast_differs(src, new):
            return new
    return None


def comment_mutation(rng, src, inline=False):
    lines = src.splitlines(keepends=True)
    i = rng.randrange(len(lines))
    text = rng.choice(COMMENTS[:-1] if i > 1 else COMMENTS)
    if inline:
        line = lines[i]
        if not line.strip():
            return None
        lines[i] = line.rstrip("\n") + "  " + text + "\n"
    else:
        target = lines[i]
        indent = target[: len(target) - len(target.lstrip())]
        lines.insert(i, indent + text + "\n")
    return "".join(lines)


def fixture_mutation(rng, src):
    lines = src.splitlines(keepends=True)
    idx = [i for i, l in enumerate(lines) if l.strip()]
    if not idx:
        return None
    i = rng.choice(idx)
    line = lines[i]
    words = line.rstrip("\n").split(" ")
    op = rng.random()
    if op < 0.4 and len(words) > 1:
        j = rng.randrange(1, len(words))
        words.insert(j, rng.choice(["x", "1", "SELECT", ")", "AS"]))
    elif op < 0.7:
        j = rng.randrange(len(words))
        w = words[j]
        if len(w) > 2:
            k = rng.randrange(len(w) - 1)
            words[j] = w[:k] + w[k + 1] + w[k] + w[k + 2:]
        else:
            words[j] = w + "y"
    elif op < 0.85:
        lines.insert(i, rng.choice(["# Keep in sync with the generator", "-- note", "# execute: false"]) + "\n")
        return "".join(lines)
    else:
        del lines[i]
        return "".join(lines)
    new_line = " ".join(words) + ("\n" if line.endswith("\n") else "")
    if new_line == line:
        return None
    lines[i] = new_line
    return "".join(lines)


def delete_line_mutation(rng, src):
    tree = ast.parse(src)
    stmts = [n for n in ast.walk(tree) if isinstance(n, ast.stmt) and n.lineno == n.end_lineno
             and not isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    lines = src.splitlines(keepends=True)
    rng.shuffle(stmts)
    for s in stmts[:30]:
        new_lines = list(lines)
        line = new_lines[s.lineno - 1]
        indent = line[: len(line) - len(line.lstrip())]
        new_lines[s.lineno - 1] = indent + "pass\n" if rng.random() < 0.5 else ""
        new = "".join(new_lines)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        if ast_differs(src, new):
            return new
    return None


def docstring_mutation(rng, src):
    tree = ast.parse(src)
    docs = [n.body[0] for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Module)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
            and isinstance(n.body[0].value.value, str)]
    if not docs:
        return None
    d = rng.choice(docs)
    lines = src.splitlines(keepends=True)
    line = lines[d.lineno - 1]
    q = line.find('"""')
    if q < 0:
        return None
    lines[d.lineno - 1] = line[: q + 3] + "Note. " + line[q + 3:]
    return "".join(lines)


def syntax_mutation(rng, src):
    lines = src.splitlines(keepends=True)
    i = rng.randrange(len(lines))
    lines[i] = lines[i].rstrip("\n") + rng.choice([" )", ":", " ="]) + "\n"
    new = "".join(lines)
    try:
        ast.parse(new)
        return None
    except SyntaxError:
        return new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--kinds", default=None, help="comma separated subset")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    os.makedirs(args.out, exist_ok=True)
    repo = args.repo

    src_files = [f for f in rel_files(repo, "sqlglot", (".py",)) if "__pycache__" not in f]
    weights = [max(1, os.path.getsize(os.path.join(repo, f)) // 2000) for f in src_files]
    test_files = rel_files(repo, "tests", (".py",))
    fixtures = [f for f in rel_files(repo, "tests/fixtures", (".sql", ".json", ".txt", ".csv")) if "/." not in f]
    other = [f for f in ["README.md", "CHANGELOG.md", "CONTRIBUTING.md"] if os.path.exists(os.path.join(repo, f))]

    kinds = [("code", 40), ("fixture", 15), ("comment", 8), ("comment_inline", 4), ("multi", 8),
             ("delete_line", 6), ("docstring", 3), ("test_edit", 5), ("syntax", 3), ("docs", 2),
             ("new_file", 2), ("delete_file", 2), ("rename_file", 1), ("config", 1), ("class_literal", 10), ("cmp_swap", 6)]
    if args.kinds:
        keep = set(args.kinds.split(","))
        kinds = [k for k in kinds if k[0] in keep]
    names, kw = zip(*kinds)

    def one(kind):
        if kind == "code":
            f = rng.choices(src_files, weights)[0]
            old = read(repo, f)
            new = token_mutation(rng, old)
            return diff(f, old, new) if new else None
        if kind == "cmp_swap":
            f = rng.choices(src_files, weights)[0]
            old = read(repo, f)
            new = cmp_swap_mutation(rng, old)
            return diff(f, old, new) if new else None
        if kind == "class_literal":
            f = rng.choices(src_files, weights)[0]
            old = read(repo, f)
            new = class_literal_mutation(rng, old)
            return diff(f, old, new) if new else None
        if kind in ("comment", "comment_inline"):
            f = rng.choices(src_files, weights)[0] if rng.random() < 0.8 else rng.choice(test_files + fixtures)
            old = read(repo, f)
            new = comment_mutation(rng, old, inline=kind == "comment_inline")
            return diff(f, old, new) if new and new != old else None
        if kind == "fixture":
            f = rng.choice(fixtures)
            old = read(repo, f)
            new = fixture_mutation(rng, old)
            return diff(f, old, new) if new and new != old else None
        if kind == "delete_line":
            f = rng.choices(src_files, weights)[0]
            old = read(repo, f)
            new = delete_line_mutation(rng, old)
            return diff(f, old, new) if new else None
        if kind == "docstring":
            f = rng.choices(src_files, weights)[0]
            old = read(repo, f)
            new = docstring_mutation(rng, old)
            return diff(f, old, new) if new else None
        if kind == "test_edit":
            f = rng.choice(test_files)
            old = read(repo, f)
            new = token_mutation(rng, old)
            return diff(f, old, new) if new else None
        if kind == "syntax":
            f = rng.choices(src_files, weights)[0]
            old = read(repo, f)
            new = syntax_mutation(rng, old)
            return diff(f, old, new) if new else None
        if kind == "docs":
            f = rng.choice(other)
            old = read(repo, f)
            return diff(f, old, old + "\nMore notes.\n")
        if kind == "new_file":
            choice = rng.choice(["sqlglot/dialects/newdialect.py", "tests/fixtures/optimizer/new_case.sql",
                                 "docs/notes.md", "sqlglot/optimizer/helpers_extra.py"])
            return diff(choice, None, "# placeholder\nVALUE = 1\n")
        if kind == "delete_file":
            f = rng.choice(fixtures + src_files[:0] + [rng.choice(src_files)])
            return diff(f, read(repo, f), None)
        if kind == "rename_file":
            f = rng.choice(fixtures)
            return diff(f, read(repo, f), read(repo, f), old_path=f, new_path=f.replace(".", "_renamed.", 1))
        if kind == "config":
            old = read(repo, "pyproject.toml")
            return diff("pyproject.toml", old, old + "\n[tool.blast_radius_example]\nvalue = 1\n")
        if kind == "multi":
            parts = []
            for k in rng.sample(["code", "comment", "fixture", "code", "comment"], rng.choice([2, 3])):
                p = None
                for _ in range(5):
                    p = one(k)
                    if p:
                        break
                if p:
                    parts.append(p)
            files = [p.split("\n", 1)[0] for p in parts]
            if len(set(files)) != len(files):
                return None
            return "".join(parts) if parts else None
        raise ValueError(kind)

    made = 0
    attempts = 0
    while made < args.count and attempts < args.count * 20:
        attempts += 1
        kind = rng.choices(names, kw)[0]
        try:
            p = one(kind)
        except (SyntaxError, UnicodeDecodeError, ValueError, tokenize.TokenError, IndexError):
            p = None
        if not p:
            continue
        cid = f"m{args.seed:02d}{made:03d}-{kind}"
        with open(os.path.join(args.out, cid + ".patch"), "w", encoding="utf-8") as fh:
            fh.write(p)
        made += 1
    print(f"wrote {made} patches to {args.out}")


if __name__ == "__main__":
    main()
