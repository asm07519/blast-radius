#!/usr/bin/env python3
"""Generate test patches against the pinned sqlglot, of the kinds seen in the sample and others.

    python3 tools/mutants.py --repo /src/sqlglot --out work/mutants --count 200 --seed 1

v2 validation corpus (PRD §10): a development set and a frozen holdout, split by
source region and edit family, each with a manifest (no labels):

    python3 tools/mutants.py --repo /src/sqlglot --split dev --out work/v2/dev \
        --manifest validation/dev.manifest.json
    python3 tools/mutants.py --repo /src/sqlglot --verify-manifest validation/holdout.manifest.json \
        [--out work/v2/holdout]

Used only to check the selector for misses; nothing from these runs is shipped, and
nothing in selector/ or the map builders may import this file.
"""

import argparse
import ast
import base64
import difflib
import hashlib
import io
import json
import os
import random
import re
import struct
import sys
import time
import tokenize
import zlib

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


# ---------------------------------------------------------------------------
# v2 validation corpus (PRD §10 "Validation matrix").
#
# Two sets of independent evaluation patches, a development set and a frozen holdout.
# They are split *by source region and edit family* before anyone looks at results:
#
# * region: a (file, coarse line bucket) key. Files of HUGE_LINES lines or more, and
#   everything under sqlglot/expressions/, are cut into BUCKET_LINES-line buckets, so
#   both sets cover parser.py, generator.py, dialects/dialect.py, expressions/*, ...
#   but never the same bucket. Smaller files are one region. New files are keyed by
#   their own path. sha256(SPLIT_SALT + key) decides the set. Every region a patch
#   touches must belong to its set.
# * family: HOLDOUT_ONLY families never appear in the development set.
#
# Everything is a pure function of (GEN_VERSION, seed, split, pinned sources), so a
# manifest's patches can be regenerated byte for byte (--verify-manifest). The
# manifest holds ids, families, files, regions and hashes; never labels or outcomes.
# ---------------------------------------------------------------------------

GEN_VERSION = "v2.1"
PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
SPLIT_SALT = "blast-radius-v2-split-1|"
HUGE_LINES = 1500
BUCKET_LINES = 500
BUCKET_OVERRIDES = {"tests/helpers.py": 40}  # small but central: give both sets a share
DEFAULT_SEEDS = {"dev": 2101, "holdout": 2102}
ID_PREFIX = {"dev": "d", "holdout": "h"}
# Families never shown to development: they stress Mode 0 (state that leaks between
# tests in suite order) and import-time narrowing (literal swaps that raise at import,
# type/equality producers that invalidate the is/== profile).
HOLDOUT_ONLY = ("literal_init_error", "shared_state", "compound_eq")
RARE_DIALECTS = ("exasol", "dremio", "solr", "druid", "dune", "tableau", "dax", "prql", "fabric",
                 "materialize", "risingwave", "drill", "doris", "teradata", "athena")
COMMON_FILES = ("sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/tokenizer_core.py", "sqlglot/tokens.py",
                "sqlglot/helper.py", "sqlglot/expressions/core.py", "sqlglot/expressions/builders.py",
                "sqlglot/optimizer/scope.py", "sqlglot/dialects/dialect.py", "sqlglot/parsers/base.py",
                "sqlglot/optimizer/qualify_columns.py", "sqlglot/optimizer/simplify.py")
LOW_LEVEL = ("sqlglot/helper.py", "sqlglot/errors.py", "sqlglot/tokens.py", "sqlglot/trie.py", "sqlglot/time.py",
             "sqlglot/_typing.py", "sqlglot/expressions/core.py", "sqlglot/tokenizer_core.py", "sqlglot/jsonpath.py",
             "sqlglot/serde.py", "sqlglot/schema.py", "sqlglot/optimizer/scope.py", "sqlglot/expressions/datatypes.py")
CYCLE_IMPORTS = ("import sqlglot.optimizer  # noqa: F401", "from sqlglot.dialects.dialect import Dialect  # noqa: F401",
                 "from sqlglot.generator import Generator  # noqa: F401", "from sqlglot import parse_one  # noqa: F401",
                 "from sqlglot.parser import Parser  # noqa: F401")
WORDS = ("alpha", "bravo", "cedar", "delta", "ember", "fjord", "grove", "harbor", "iris", "juniper", "kestrel",
         "lumen", "maple", "nimbus", "orbit", "pebble", "quartz", "raven", "sierra", "tundra")


class Skip(Exception):
    """This attempt cannot produce a patch; try again with another random draw."""


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def split_of(key):
    return "dev" if int(_sha((SPLIT_SALT + key).encode())[:8], 16) % 2 == 0 else "holdout"


def _lines(src):
    """Lines split on newline only (str.splitlines also splits on form feed, U+2028, ...)."""
    parts = src.split("\n")
    return [x + "\n" for x in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _udiff(path, old, new):
    """Like diff(), on newline-split lines."""
    head = f"diff --git a/{path} b/{path}\n"
    if old is None:
        head += "new file mode 100644\n"
        a, b, fromf, tof = [], _lines(new), "/dev/null", f"b/{path}"
    elif new is None:
        head += "deleted file mode 100644\n"
        a, b, fromf, tof = _lines(old), [], f"a/{path}", "/dev/null"
    else:
        a, b, fromf, tof = _lines(old), _lines(new), f"a/{path}", f"b/{path}"
    return head + "".join(_fix(x) for x in difflib.unified_diff(a, b, fromf, tof, n=3))


def _parses(src):
    try:
        ast.parse(src)
        return True
    except (SyntaxError, ValueError):
        return False


def _seg(lines, node):
    """Source text of a node that starts and ends on one line (ast offsets are UTF-8 bytes)."""
    if node.lineno != node.end_lineno:
        raise Skip("multi-line node")
    enc = lines[node.lineno - 1].encode("utf-8")
    return enc[node.col_offset:node.end_col_offset].decode("utf-8")


def _replace(src, lineno, col, end_col, text):
    lines = _lines(src)
    enc = lines[lineno - 1].encode("utf-8")
    lines[lineno - 1] = (enc[:col] + text.encode("utf-8") + enc[end_col:]).decode("utf-8")
    return "".join(lines)


def _replace_node(src, node, text):
    if node.lineno != node.end_lineno:
        raise Skip("multi-line node")
    return _replace(src, node.lineno, node.col_offset, node.end_col_offset, text)


def _insert(src, before, block, indent):
    """Insert `block` (lines without indentation) before 1-based line `before`."""
    lines = _lines(src)
    lines[before - 1:before - 1] = [(indent + line if line else "") + "\n" for line in block]
    return "".join(lines)


def _stmt_indent(lines, stmt):
    line = lines[stmt.lineno - 1]
    prefix = line.encode("utf-8")[:stmt.col_offset].decode("utf-8", "replace")
    if prefix.strip():
        raise Skip("statement shares its line")
    return prefix


def _is_doc(stmt):
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _real_body(func):
    body = func.body[1:] if func.body and _is_doc(func.body[0]) else func.body
    return [s for s in body if s.lineno != func.lineno]


def _params(func):
    a = func.args
    return [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs if x.arg not in ("self", "cls")]


def _blocks(node):
    """Every statement list inside a function body (body, orelse, handlers, ...)."""
    out = []
    for n in ast.walk(node):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(n, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                out.append(block)
        for h in getattr(n, "handlers", []) or []:
            out.append(h.body)
    return out


def _functions(tree):
    out = []

    def visit(node, cls):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((ch, cls))
                visit(ch, None)
            elif isinstance(ch, ast.ClassDef):
                visit(ch, ch)
            else:
                visit(ch, cls)

    visit(tree, None)
    return out


def _call_time(tree):
    """ids of nodes inside function or lambda bodies (code that runs at call time)."""
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for s in n.body:
                ids.update(id(x) for x in ast.walk(s))
        elif isinstance(n, ast.Lambda):
            ids.update(id(x) for x in ast.walk(n.body))
    return ids


def _init_time_values(tree):
    """(owner, stmt) for assignments directly in a module or class body."""
    out = []
    for owner in [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for s in owner.body:
            if isinstance(s, (ast.Assign, ast.AnnAssign)) and s.value is not None:
                out.append((owner, s))
    return out


def _walk_no_lambda(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, ast.Lambda):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(n))))


def _top_import_end(tree):
    last = 0
    for s in tree.body:
        if isinstance(s, (ast.Import, ast.ImportFrom)):
            last = s.end_lineno
        elif _is_doc(s) and last == 0:
            continue
        else:
            break
    return last


def _flip_const(v):
    if isinstance(v, bool):
        return repr(not v)
    if v is None:
        return "''"
    if isinstance(v, int):
        return str(v + 1)
    if isinstance(v, float):
        return repr(v + 1.0)
    if isinstance(v, str) and len(v) < 60 and "\n" not in v and "\\" not in v:
        return repr(v + "x")
    raise Skip("constant")


# --- deterministic binary payloads (no zlib version dependence) ---

def _stored_deflate(data):
    out = bytearray()
    if not data:
        return b"\x01\x00\x00\xff\xff"
    for i in range(0, len(data), 65535):
        chunk = data[i:i + 65535]
        final = 1 if i + 65535 >= len(data) else 0
        out += bytes([final]) + struct.pack("<HH", len(chunk), len(chunk) ^ 0xFFFF) + chunk
    return bytes(out)


def _zlib_stored(data):
    return b"\x78\x01" + _stored_deflate(data) + struct.pack(">I", zlib.adler32(data) & 0xFFFFFFFF)


def _gzip_stored(data):
    return (b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff" + _stored_deflate(data)
            + struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF))


def _git_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _b85_block(data):
    z = _zlib_stored(data)
    out = []
    for i in range(0, len(z), 52):
        chunk = z[i:i + 52]
        n = len(chunk)
        out.append((chr(ord("A") + n - 1) if n <= 26 else chr(ord("a") + n - 27)) + base64.b85encode(chunk, pad=True).decode())
    return "literal %d\n" % len(data) + "".join(line + "\n" for line in out) + "\n"


def binary_diff(path, old, new):
    """A `GIT binary patch` stanza (literal forward and reverse) with full index hashes."""
    zero = "0" * 40
    head = f"diff --git a/{path} b/{path}\n"
    if old is None:
        head += f"new file mode 100644\nindex {zero}..{_git_sha(new)}\n"
    elif new is None:
        head += f"deleted file mode 100644\nindex {_git_sha(old)}..{zero}\n"
    else:
        head += f"index {_git_sha(old)}..{_git_sha(new)} 100644\n"
    return (head + "GIT binary patch\n" + _b85_block(new or b"") + _b85_block(old or b"")).encode("ascii")


def rename_diff(old_path, new_path, old, new):
    head = f"diff --git a/{old_path} b/{new_path}\n"
    if old == new:
        return head + f"similarity index 100%\nrename from {old_path}\nrename to {new_path}\n"
    a, b = _lines(old), _lines(new)
    sim = int(100 * difflib.SequenceMatcher(None, a, b, autojunk=False).ratio())
    body = "".join(_fix(x) for x in difflib.unified_diff(a, b, f"a/{old_path}", f"b/{new_path}", n=3))
    return head + f"similarity index {sim}%\nrename from {old_path}\nrename to {new_path}\n" + body


def mode_diff(path, old=None, new=None, old_mode="100644", new_mode="100755"):
    head = f"diff --git a/{path} b/{path}\nold mode {old_mode}\nnew mode {new_mode}\n"
    if old is None or new is None or old == new:
        return head
    body = "".join(_fix(x) for x in difflib.unified_diff(_lines(old), _lines(new), f"a/{path}", f"b/{path}", n=3))
    return head + body


def _touched(old, new):
    """1-based old-file line ranges a text edit touches (insertions: the line before)."""
    sm = difflib.SequenceMatcher(None, _lines(old), _lines(new), autojunk=False)
    out = []
    for tag, i1, i2, _, _ in sm.get_opcodes():
        if tag == "equal":
            continue
        out.append([max(i1, 1), max(i2, i1 + 1, 1)] if tag != "insert" else [max(i1, 1), max(i1, 1)])
    return out


class Cand:
    """One generated patch: its stanzas plus what it touches."""

    def __init__(self, ctx, family, detail):
        self.ctx = ctx
        self.family = family
        self.detail = detail
        self.parts = []
        self.files = []
        self.regions = set()
        self.labellable = True
        self.tags = []

    def _note(self, path, op, lines, regions):
        self.files.append({"path": path, "op": op, "lines": lines})
        self.regions.update(regions)

    def modify(self, path, old, new, semantic=True, syntax_error=False):
        if new is None or new == old:
            raise Skip("no change")
        if path.endswith(".py") and _parses(new) == syntax_error:
            raise Skip("parse result not the intended one")
        if semantic and path.endswith(".py") and not ast_differs(old, new):
            raise Skip("AST unchanged")
        lines = _touched(old, new)
        self._note(path, "modify", lines, {self.ctx.region(path, ln) for r in lines for ln in range(r[0], r[1] + 1)})
        self.parts.append(_udiff(path, old, new).encode("utf-8"))
        return self

    def add(self, path, content, raw=None):
        if self.ctx.exists(path):
            raise Skip("exists")
        self._note(path, "add", [], {self.ctx.region(path, 1, new=True)})
        self.parts.append(raw if raw is not None else _udiff(path, None, content).encode("utf-8"))
        return self

    def delete(self, path):
        self._note(path, "delete", [], self.ctx.all_regions(path))
        if path.endswith(".gz"):
            self.parts.append(binary_diff(path, self.ctx.data(path), None))
        else:
            self.parts.append(_udiff(path, self.ctx.text(path), None).encode("utf-8"))
        return self

    def rename(self, old_path, new_path, new=None):
        if self.ctx.exists(new_path):
            raise Skip("exists")
        old = self.ctx.text(old_path)
        self._note(old_path, "rename", [], self.ctx.all_regions(old_path) | {self.ctx.region(new_path, 1, new=True)})
        self.files[-1]["to"] = new_path
        self.parts.append(rename_diff(old_path, new_path, old, old if new is None else new).encode("utf-8"))
        return self

    def mode(self, path, new=None):
        old = self.ctx.text(path)
        self._note(path, "mode", _touched(old, new) if new is not None else [], self.ctx.all_regions(path))
        self.parts.append(mode_diff(path, old, new).encode("utf-8"))
        return self

    def binary(self, path, new):
        old = self.ctx.data(path)
        if old == new:
            raise Skip("no change")
        self._note(path, "binary", [], self.ctx.all_regions(path))
        self.parts.append(binary_diff(path, old, new))
        return self

    def data(self):
        return b"".join(self.parts)


def _docs(repo):
    root = [p for p in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md")
            if os.path.exists(os.path.join(repo, p))]
    return root + rel_files(repo, "posts", (".md",))


class Ctx:
    """The pinned source tree, read once, plus the split being generated."""

    def __init__(self, repo, split):
        self.repo = repo
        self.split = split
        self._text, self._data, self._tree, self._nlines, self._cache = {}, {}, {}, {}, {}
        self.src = rel_files(repo, "sqlglot", (".py",))
        self.weights = [max(1, os.path.getsize(os.path.join(repo, f)) // 2000) for f in self.src]
        self.tests_py = rel_files(repo, "tests", (".py",))
        self.test_modules = [f for f in self.tests_py if os.path.basename(f).startswith("test_")]
        self.fixtures = [f for f in rel_files(repo, "tests/fixtures", (".sql", ".json", ".txt", ".csv")) if "/." not in f]
        self.gz_small = [f for f in rel_files(repo, "tests/fixtures", (".gz",)) if os.path.getsize(os.path.join(repo, f)) < 2000]
        self.dialect_code = [f for f in self.src if f.startswith(("sqlglot/parsers/", "sqlglot/generators/", "sqlglot/dialects/"))
                             and os.path.basename(f) not in ("__init__.py", "dialect.py", "base.py")]
        self.rare = [f for f in self.src if any(f.endswith(f"/{d}.py") for d in RARE_DIALECTS)] + \
            [f for f in self.src if f.startswith("sqlglot/executor/")] + \
            [f"sqlglot/{m}.py" for m in ("anonymize", "diff", "lineage", "planner", "serde", "jsonpath")]
        self.common = [f for f in COMMON_FILES if f in self.src]
        self.docs = _docs(repo)

    # files
    def exists(self, path):
        return os.path.exists(os.path.join(self.repo, path))

    def data(self, path):
        if path not in self._data:
            with open(os.path.join(self.repo, path), "rb") as fh:
                self._data[path] = fh.read()
        return self._data[path]

    def text(self, path):
        if path not in self._text:
            self._text[path] = self.data(path).decode("utf-8")
        return self._text[path]

    def tree(self, path):
        if path not in self._tree:
            self._tree[path] = ast.parse(self.text(path))
        return self._tree[path]

    def nlines(self, path):
        if path not in self._nlines:
            self._nlines[path] = self.data(path).count(b"\n") + 1 if self.exists(path) else 0
        return self._nlines[path]

    def ops(self, path):
        return self.cached(("ops", path), lambda: op_sites(self.text(path), self.tree(path)))

    def cached(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # regions
    def bucket(self, path):
        if path in BUCKET_OVERRIDES:
            return BUCKET_OVERRIDES[path]
        if self.nlines(path) >= HUGE_LINES or path.startswith("sqlglot/expressions/"):
            return BUCKET_LINES
        return 0

    def region(self, path, line, new=False):
        size = 0 if new else self.bucket(path)
        return f"{path}#{(max(line, 1) - 1) // size}" if size else path

    def all_regions(self, path):
        size = self.bucket(path)
        if not size:
            return {path}
        return {f"{path}#{k}" for k in range((self.nlines(path) - 1) // size + 1)}

    def ok(self, path, line, new=False):
        return split_of(self.region(path, line, new)) == self.split

    def ok_span(self, path, lo, hi):
        return all(self.ok(path, ln) for ln in range(lo, hi + 1))

    def ok_file(self, path):
        return all(split_of(r) == self.split for r in self.all_regions(path))

    def pick_src(self, rng, pool=None):
        if pool is None:
            return rng.choices(self.src, self.weights)[0]
        return rng.choice(pool)


# --- site finders -----------------------------------------------------------

CMP_MAP = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "==", "is": "==", "is not": "!=",
           "in": "not in", "not in": "in"}
CMP_CAT = {"<": "lt", "<=": "lt", ">": "lt", ">=": "lt", "==": "eq", "!=": "eq", "is": "is", "is not": "is",
           "in": "in", "not in": "in"}


def op_sites(src, tree):
    """Single-operator edit sites at call time: (category, lineno, col, end_col, new text, desc)."""
    lines = _lines(src)
    call = _call_time(tree)
    out = []
    for n in ast.walk(tree):
        if id(n) not in call:
            continue
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and n.left.end_lineno == n.comparators[0].lineno:
            ln = n.left.end_lineno
            enc = lines[ln - 1].encode("utf-8")
            a, b = n.left.end_col_offset, n.comparators[0].col_offset
            mid = enc[a:b].decode("utf-8", "replace")
            op = " ".join(mid.replace("(", " ").replace(")", " ").split())
            if op in CMP_MAP:
                out.append((CMP_CAT[op], ln, a, b, mid.replace(op, CMP_MAP[op], 1) if op in mid else None,
                            f"{op}->{CMP_MAP[op]}"))
        elif isinstance(n, ast.keyword) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, bool):
            v = n.value
            cat = "copy" if n.arg == "copy" else "kwbool"
            out.append((cat, v.lineno, v.col_offset, v.end_col_offset, repr(not v.value), f"{n.arg}={v.value}->{not v.value}"))
        elif isinstance(n, ast.BoolOp) and len(n.values) == 2 and n.values[0].end_lineno == n.values[1].lineno:
            ln = n.values[0].end_lineno
            enc = lines[ln - 1].encode("utf-8")
            a, b = n.values[0].end_col_offset, n.values[1].col_offset
            mid = enc[a:b].decode("utf-8", "replace")
            word = "and" if isinstance(n.op, ast.And) else "or"
            other = "or" if word == "and" else "and"
            if re.fullmatch(rf"[\s)]*{word}[\s(]*", mid):
                out.append(("boolop", ln, a, b, mid.replace(word, other, 1), f"{word}->{other}"))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not) and n.lineno == n.operand.lineno:
            enc = lines[n.lineno - 1].encode("utf-8")
            if enc[n.col_offset:n.operand.col_offset].strip() == b"not":
                out.append(("not", n.lineno, n.col_offset, n.operand.col_offset, "", "not->(removed)"))
    return [s for s in out if s[4] is not None]


SWAP = {"is": "==", "is not": "!=", "==": "is", "!=": "is not"}


def cmp_swap_sites(src, tree):
    """is <-> == and is not <-> != swaps at call time (what the type-profile shortcut covers)."""
    out = []
    lines = _lines(src)
    for cat, ln, a, b, _, desc in op_sites(src, tree):
        op = desc.split("->")[0]
        if cat in ("is", "eq") and op in SWAP:
            enc = lines[ln - 1].encode("utf-8")
            mid = enc[a:b].decode("utf-8", "replace")
            out.append(("swap", ln, a, b, mid.replace(op, SWAP[op], 1), f"{op}->{SWAP[op]}"))
    return out


def _apply_site(src, site):
    _, ln, a, b, text, _ = site
    return _replace(src, ln, a, b, text)


# --- families -----------------------------------------------------------------

def fam_m0_op(ctx, rng, detail):
    """Single-operator edits in dialect-specific parser/generator code (what Mode 0 acts on)."""
    def index():
        out = {}
        for f in ctx.dialect_code:
            for s in ctx.ops(f):
                if ctx.ok(f, s[1]):
                    out.setdefault(s[0], {}).setdefault(f, []).append(s)
        return out

    sites = ctx.cached("m0", index).get(detail)
    if not sites:
        raise Skip("no site")
    f = rng.choice(sorted(sites))
    site = rng.choice(sites[f])
    old = ctx.text(f)
    c = Cand(ctx, "m0_op", f"{detail}:{site[5]}")
    return c.modify(f, old, _apply_site(old, site))


def _pick_function(ctx, rng, pool=None, need_params=False, files=None, tries=12):
    for _ in range(tries):
        f = rng.choice(files) if files else ctx.pick_src(rng, pool)
        tree = ctx.tree(f)
        funcs = [(fn, cls) for fn, cls in _functions(tree) if _real_body(fn) and (_params(fn) or not need_params)]
        rng.shuffle(funcs)
        for fn, cls in funcs[:20]:
            body = _real_body(fn)
            if ctx.ok(f, body[0].lineno):
                return f, fn, cls
    raise Skip("no function")


def _cond(rng, p):
    return rng.choice([f"{p} is None", f"not {p}", f"isinstance({p}, str)", f"{p} is not None"])


def fam_insert_branch(ctx, rng, detail):
    """Inserted branches/statements with no old line event."""
    f, fn, cls = _pick_function(ctx, rng, need_params=detail != "after_return_dead")
    old = ctx.text(f)
    lines = _lines(old)
    body = _real_body(fn)
    params = _params(fn)
    p = rng.choice(params) if params else None
    if detail == "entry_return":
        s = body[0]
        block = [f"if {_cond(rng, p)}:", "    " + rng.choice(["return None", f"return {p}"])]
    elif detail == "entry_raise":
        s = body[0]
        block = [f"if {_cond(rng, p)}:", f'    raise ValueError("unexpected {p}")']
    elif detail == "middle_return":
        stmts = [x for blk in _blocks(fn) for x in blk if x is not body[0] and x.lineno != fn.lineno and not _is_doc(x)]
        if not stmts:
            raise Skip("short")
        s = rng.choice(stmts)
        block = [f"if {_cond(rng, p)}:", "    return None"]
    elif detail == "after_return_dead":
        rets = [blk[-1] for blk in _blocks(fn) if isinstance(blk[-1], ast.Return)]
        if not rets:
            raise Skip("no return")
        r = rng.choice(rets)
        indent = _stmt_indent(lines, r)
        if not ctx.ok(f, r.end_lineno):
            raise Skip("region")
        new = _insert(old, r.end_lineno + 1, [rng.choice(["return None", 'raise AssertionError("unreachable")', "pass"])], indent)
        return Cand(ctx, "insert_branch", detail).modify(f, old, new)
    elif detail == "after_if_return":
        cands = []
        for blk in _blocks(fn):
            for i, x in enumerate(blk[:-1]):
                if isinstance(x, ast.If) and x.body and isinstance(x.body[-1], (ast.Return, ast.Raise)) and not x.orelse:
                    cands.append(blk[i + 1])
        if not cands:
            raise Skip("no guard")
        s = rng.choice(cands)
        block = [f"if {_cond(rng, p)}:", "    return None"]
    elif detail in ("loop_continue", "loop_break"):
        loops = [n for n in ast.walk(fn) if isinstance(n, ast.For) and isinstance(n.target, ast.Name)]
        if not loops:
            raise Skip("no loop")
        loop = rng.choice(loops)
        s = loop.body[0]
        v = loop.target.id
        block = [f"if not {v}:", "    continue"] if detail == "loop_continue" else [f"if {v} is None:", "    break"]
    elif detail == "early_stmt":
        s = body[0]
        opts = [f"{p} = {p}", f"assert {p} is not None", f"{p} = None", f"_ = len(str({p}))"]
        if cls is not None and fn.args.args and fn.args.args[0].arg == "self":
            opts.append("self._br_seen = True")
        block = [rng.choice(opts)]
    else:
        raise ValueError(detail)
    if not ctx.ok(f, s.lineno):
        raise Skip("region")
    new = _insert(old, s.lineno, block, _stmt_indent(lines, s))
    return Cand(ctx, "insert_branch", detail).modify(f, old, new)


def _init_constants(ctx, f, keys=False):
    """Constants in module/class-body assignment values (not inside lambdas)."""
    tree = ctx.tree(f)
    out = []
    fstr = {id(x) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for x in ast.walk(n)}
    for _, s in _init_time_values(tree):
        key_ids = set()
        for n in _walk_no_lambda(s.value):
            if isinstance(n, ast.Dict):
                key_ids.update(id(k) for k in n.keys if k is not None)
        for n in _walk_no_lambda(s.value):
            if isinstance(n, ast.Constant) and n.lineno == n.end_lineno and not isinstance(n.value, bytes) \
                    and (id(n) in key_ids) == keys and id(n) not in fstr and ctx.ok(f, n.lineno):
                out.append(n)
    return out


def fam_literal_init_error(ctx, rng, detail):
    """Edits that look like literal swaps but raise while the module/class body runs."""
    for _ in range(10):
        f = ctx.pick_src(rng)
        old = ctx.text(f)
        tree = ctx.tree(f)
        if detail in ("neg_str", "unhashable_dict", "tuple_list_key", "unhashable_set", "neg_tuple"):
            consts = [n for n in _init_constants(ctx, f) if not isinstance(n.value, str) or len(n.value) < 60]
            if not consts:
                continue
            n = rng.choice(consts)
            text = {"neg_str": "-'x'", "unhashable_dict": "{[]: 1}", "tuple_list_key": "{(1, []): 2}",
                    "unhashable_set": "{[]}", "neg_tuple": "-(1,)"}[detail]
            new = _replace_node(old, n, text)
        elif detail == "key_to_list":
            keys = []
            for _, s in _init_time_values(tree):
                for d in _walk_no_lambda(s.value):
                    if isinstance(d, ast.Dict):
                        keys += [k for k in d.keys if k is not None and k.lineno == k.end_lineno and ctx.ok(f, k.lineno)]
            if not keys:
                continue
            k = rng.choice(keys)
            new = _replace_node(old, k, "[" + _seg(_lines(old), k) + "]")
        elif detail == "member_to_list":
            elts = []
            for _, s in _init_time_values(tree):
                for d in _walk_no_lambda(s.value):
                    if isinstance(d, ast.Set):
                        elts += [e for e in d.elts if e.lineno == e.end_lineno and ctx.ok(f, e.lineno)]
            if not elts:
                continue
            e = rng.choice(elts)
            new = _replace_node(old, e, "[" + _seg(_lines(old), e) + "]")
        else:
            raise ValueError(detail)
        return Cand(ctx, "literal_init_error", detail).modify(f, old, new)
    raise Skip("no site")


def _tables(ctx, f):
    """Module/class-body assignments whose value is a dict/set/tuple/list literal."""
    out = []
    for owner, s in _init_time_values(ctx.tree(f)):
        v = s.value
        if isinstance(v, (ast.Dict, ast.Set, ast.Tuple, ast.List)):
            n = len(v.keys) if isinstance(v, ast.Dict) else len(v.elts)
            if n:
                out.append((owner, s, v, n))
    return out


def _remove_item(src, lines, start_line, start_col, end_line, end_col):
    """Remove one element `start..end` of a literal: its whole line(s) when alone, else inline."""
    first = lines[start_line - 1]
    last = lines[end_line - 1]
    before = first.encode("utf-8")[:start_col].decode("utf-8", "replace")
    after = last.encode("utf-8")[end_col:].decode("utf-8", "replace")
    if not before.strip() and after.strip() in (",", ""):
        out = lines[:start_line - 1] + lines[end_line:]
        return "".join(out)
    if start_line != end_line:
        raise Skip("multi-line inline element")
    enc = first.encode("utf-8")
    rest = enc[end_col:]
    if rest.startswith(b", "):
        return "".join(lines[:start_line - 1] + [(enc[:start_col] + rest[2:]).decode("utf-8")] + lines[start_line:])
    head = enc[:start_col]
    if head.endswith(b", "):
        return "".join(lines[:start_line - 1] + [(head[:-2] + rest).decode("utf-8")] + lines[start_line:])
    raise Skip("cannot remove")


def _sibling_attr(ctx, f, node, taken):
    """Another `Base.NAME` used in the file with the same base, for a new key/member."""
    if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
        return None
    base = node.value.id
    names = sorted({n.attr for n in ast.walk(ctx.tree(f)) if isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name) and n.value.id == base} - taken)
    return f"{base}.{names[0 if len(names) == 1 else hash_pick(names, node)]}" if names else None


def hash_pick(names, node):
    return int(_sha(f"{node.lineno}:{node.col_offset}".encode())[:6], 16) % len(names)


def _elem_text(ctx, f, lines, node, taken):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return repr(node.value + "_BR")
    alt = _sibling_attr(ctx, f, node, taken)
    if alt:
        return alt
    raise Skip("no new element")


def fam_class_table(ctx, rng, detail):
    """Class-attribute/table literal edits of many shapes, including large tables."""
    large = detail.startswith("large_")
    op = detail[6:] if large else detail
    for _ in range(12):
        f = ctx.pick_src(rng)
        old = ctx.text(f)
        lines = _lines(old)
        if op in ("bool_flip", "num", "str"):
            want = {"bool_flip": bool, "num": int, "str": str}[op]
            consts = [n for n in _init_constants(ctx, f) if type(n.value) is want]
            if not consts:
                continue
            n = rng.choice(consts)
            return Cand(ctx, "class_table", detail).modify(f, old, _replace_node(old, n, _flip_const(n.value)))
        tables = [t for t in _tables(ctx, f) if (t[3] >= 30) == large or (not large and t[3] < 30)]
        kind = {"dict": ast.Dict, "unpack": ast.Dict, "set": ast.Set, "seq": (ast.Tuple, ast.List)}[op.split("_")[0]]
        tables = [t for t in tables if isinstance(t[2], kind)]
        if not tables:
            continue
        _, s, v, _ = rng.choice(tables)
        if isinstance(v, ast.Dict):
            entries = list(zip(v.keys, v.values))
            if op == "unpack_remove":
                entries = [(k, x) for k, x in entries if k is None]
            elif op != "dict_add":
                entries = [(k, x) for k, x in entries if k is not None]
            if not entries:
                continue
            k, x = rng.choice(entries)
            start = (x.lineno, x.col_offset - 2) if k is None else (k.lineno, k.col_offset)
            if not ctx.ok_span(f, start[0], x.end_lineno):
                continue
            if op in ("dict_remove", "unpack_remove"):
                new = _remove_item(old, lines, start[0], start[1], x.end_lineno, x.end_col_offset)
            elif op == "dict_add":
                if k is None or k.lineno != x.end_lineno:
                    continue
                taken = {kk.attr for kk in v.keys if isinstance(kk, ast.Attribute)}
                key = _elem_text(ctx, f, lines, k, taken)
                entry = f"{key}: {_seg(lines, x)},"
                ind = _stmt_indent(lines, k) if lines[k.lineno - 1].encode()[:k.col_offset].strip() == b"" else None
                if ind is None:
                    continue
                new = _insert(old, x.end_lineno + 1, [entry], ind)
            elif op == "dict_value":
                others = [xx for kk, xx in entries if xx is not x and xx.lineno == xx.end_lineno]
                if not others or x.lineno != x.end_lineno:
                    continue
                alt = _seg(lines, rng.choice(others))
                if alt == _seg(lines, x):
                    continue
                new = _replace_node(old, x, alt)
            else:
                raise ValueError(detail)
        else:
            elts = [e for e in v.elts if not isinstance(e, ast.Starred)]
            if not elts:
                continue
            e = rng.choice(elts)
            if not ctx.ok_span(f, e.lineno, e.end_lineno):
                continue
            if op.endswith("_remove"):
                if len(v.elts) == 1 and isinstance(v, ast.Tuple):
                    continue
                new = _remove_item(old, lines, e.lineno, e.col_offset, e.end_lineno, e.end_col_offset)
            else:
                if e.lineno != e.end_lineno:
                    continue
                taken = {x.attr for x in v.elts if isinstance(x, ast.Attribute)}
                text = _elem_text(ctx, f, lines, e, taken)
                new = _replace(old, e.lineno, e.end_col_offset, e.end_col_offset, ", " + text)
        return Cand(ctx, "class_table", detail).modify(f, old, new)
    raise Skip("no table")


def fam_import_edit(ctx, rng, detail):
    for _ in range(10):
        pool = LOW_LEVEL if detail == "cycle" else None
        f = ctx.pick_src(rng, [p for p in pool if p in ctx.src] if pool else None)
        if detail in ("remove_used", "remove_name", "alter_name", "alter_module") and rng.random() < 0.15:
            f = rng.choice(ctx.tests_py)
        old = ctx.text(f)
        tree = ctx.tree(f)
        lines = _lines(old)
        imports = [s for s in tree.body if isinstance(s, (ast.Import, ast.ImportFrom))
                   and not (isinstance(s, ast.ImportFrom) and s.module == "__future__") and ctx.ok(f, s.lineno)]
        end = _top_import_end(tree)
        if detail == "remove_used":
            single = [s for s in imports if len(s.names) == 1 and s.lineno == s.end_lineno]
            if not single:
                continue
            s = rng.choice(single)
            new = "".join(lines[:s.lineno - 1] + lines[s.lineno:])
        elif detail == "remove_name":
            multi = [s for s in imports if len(s.names) >= 2]
            if not multi:
                continue
            s = rng.choice(multi)
            a = rng.choice(s.names)
            new = _remove_item(old, lines, a.lineno, a.col_offset, a.end_lineno, a.end_col_offset)
        elif detail == "alter_name":
            froms = [s for s in imports if isinstance(s, ast.ImportFrom)]
            if not froms:
                continue
            a = rng.choice(rng.choice(froms).names)
            if a.name == "*":
                continue
            enc = lines[a.lineno - 1].encode("utf-8")
            if enc[a.col_offset:a.col_offset + len(a.name)].decode() != a.name:
                continue
            new = _replace(old, a.lineno, a.col_offset, a.col_offset + len(a.name), a.name + "_br")
        elif detail == "alter_module":
            froms = [s for s in imports if isinstance(s, ast.ImportFrom) and s.module and s.module.count(".") >= 2]
            if not froms:
                continue
            s = rng.choice(froms)
            line = lines[s.lineno - 1]
            if f"from {s.module} import" not in line:
                continue
            new = old.replace(line, line.replace(s.module, s.module.rsplit(".", 1)[0], 1), 1)
        elif detail in ("missing_module", "guarded_missing", "cycle", "typing_only"):
            if not end or not ctx.ok(f, end):
                continue
            if detail == "missing_module":
                block = ["import sqlglot_br_missing  # noqa: F401"]
            elif detail == "guarded_missing":
                block = ["try:", "    import sqlglot_br_missing  # noqa: F401", "except ImportError:",
                         "    sqlglot_br_missing = None"]
            elif detail == "cycle":
                block = [rng.choice(CYCLE_IMPORTS)]
            else:
                tmod = "t" if re.search(r"^import typing as t$", old, re.M) else None
                if not tmod:
                    continue
                block = ["if t.TYPE_CHECKING:", "    from sqlglot.br_missing import Missing  # noqa: F401"]
            new = _insert(old, end + 1, block, "")
        elif detail == "lazy_missing":
            f2, fn, _ = _pick_function(ctx, rng, files=[f])
            s = _real_body(fn)[0]
            new = _insert(old, s.lineno, ["import sqlglot_br_missing  # noqa: F401"], _stmt_indent(lines, s))
        else:
            raise ValueError(detail)
        return Cand(ctx, "import_edit", detail).modify(f, old, new)
    raise Skip("no import")


TEST_PASS = '''import unittest

from sqlglot import parse_one


class TestBr{W}(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(parse_one("SELECT 1").sql(), "SELECT 1")
'''
TEST_FAIL = '''import unittest

from sqlglot import parse_one


class TestBr{W}(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(parse_one("SELECT 1").sql(), "SELECT 2")
'''
TEST_COLLECT_ERROR = '''import unittest

import sqlglot_br_missing  # noqa: F401


class TestBr{W}(unittest.TestCase):
    def test_nothing(self):
        pass
'''


def fam_file_ops(ctx, rng, detail):
    w = rng.choice(WORDS)
    c = Cand(ctx, "file_ops", detail)
    small_src = [f for f in ctx.src if not ctx.bucket(f) and not f.endswith("__init__.py") and ctx.ok_file(f)]
    if detail in ("shadow_pkg_empty", "shadow_pkg_copy"):
        f = rng.choice(small_src)
        body = '"""Shadow package."""\n' if detail == "shadow_pkg_empty" else ctx.text(f)
        c.add(f[:-3] + "/__init__.py", body)
        c.files[-1]["shadows"] = f
        c.regions.add(f)
        return c
    if detail in ("new_test_pass", "new_test_fail", "new_test_collect_error"):
        tpl = {"new_test_pass": TEST_PASS, "new_test_fail": TEST_FAIL, "new_test_collect_error": TEST_COLLECT_ERROR}[detail]
        d = rng.choice(["tests", "tests/dialects"])
        return c.add(f"{d}/test_br_{w}.py", tpl.replace("{W}", w.capitalize()))
    if detail == "new_top_module":
        return c.add(rng.choice([f"br_{w}.py", f"tests/br_{w}.py"]), f"VALUE = {len(w)}\n")
    if detail == "shadow_thirdparty":
        name = rng.choice(["duckdb", "pytz", "pandas", "dateutil"])
        body = rng.choice(['raise ImportError("shadowed by the repository root")\n', "VALUE = 1\n"])
        return c.add(f"{name}.py", body)
    if detail in ("new_sqlglot_module", "new_sqlglot_module_raises"):
        d = rng.choice(["sqlglot", "sqlglot/optimizer", "sqlglot/dialects", "sqlglot/executor", "sqlglot/generators"])
        body = f"VALUE = {len(w)}\n" if detail == "new_sqlglot_module" else 'raise RuntimeError("br: import-time failure")\n'
        return c.add(f"{d}/br_{w}.py", body)
    if detail == "new_dialect_module":
        return c.add(f"sqlglot/dialects/br{w}.py",
                     "from sqlglot.dialects.dialect import Dialect\n\n\n"
                     f"class Br{w.capitalize()}(Dialect):\n    pass\n")
    if detail == "new_fixture":
        d = rng.choice(["tests/fixtures", "tests/fixtures/optimizer", "tests/fixtures/optimizer/tpc-h"])
        return c.add(f"{d}/br_{w}.sql", "SELECT 1;\nSELECT 1;\n")
    if detail == "delete_module":
        return c.delete(rng.choice(small_src))
    if detail == "delete_test":
        return c.delete(rng.choice([f for f in ctx.tests_py if ctx.ok_file(f)]))
    if detail == "delete_fixture":
        return c.delete(rng.choice([f for f in ctx.fixtures if ctx.ok_file(f)]))
    if detail == "delete_fixture_gz":
        return c.delete(rng.choice([f for f in ctx.gz_small if ctx.ok_file(f)]))
    if detail == "rename_module":
        f = rng.choice(small_src)
        return c.rename(f, f[:-3] + "_old.py")
    if detail == "rename_fixture":
        f = rng.choice([f for f in ctx.fixtures if ctx.ok_file(f)])
        return c.rename(f, f.replace(".", "_renamed.", 1))
    if detail == "rename_edit":
        f = rng.choice(small_src)
        old = ctx.text(f)
        return c.rename(f, f[:-3] + "_v2.py", old + "\n# Moved.\n")
    if detail == "mode_exec":
        return c.mode(rng.choice(small_src + [f for f in ctx.tests_py if ctx.ok_file(f)]))
    if detail == "mode_with_edit":
        f = rng.choice(small_src)
        old = ctx.text(f)
        new = token_mutation(rng, old)
        if not new:
            raise Skip("no token")
        return c.mode(f, new)
    raise ValueError(detail)


CONFTESTS = {
    "conftest_fixture": "import pytest\n\n\n@pytest.fixture\ndef br_value():\n    return 1\n",
    "conftest_autouse": "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _br_autouse():\n    yield\n",
    "conftest_raise": 'raise RuntimeError("br: broken conftest")\n',
    "conftest_reverse": "def pytest_collection_modifyitems(session, config, items):\n    items.reverse()\n",
    "conftest_deselect": "def pytest_collection_modifyitems(session, config, items):\n"
                         "    items[:] = [item for item in items if \"snowflake\" not in item.nodeid]\n",
}


def fam_hook_config(ctx, rng, detail):
    c = Cand(ctx, "hook_config", detail)
    if detail in CONFTESTS:
        locs = [p for p in ("conftest.py", "tests/conftest.py", "tests/dialects/conftest.py") if ctx.ok(p, 1, new=True)]
        if not locs:
            raise Skip("region")
        return c.add(rng.choice(locs), CONFTESTS[detail])
    if detail.startswith("pyproject_"):
        old = ctx.text("pyproject.toml")
        extra = {"pyproject_filterwarnings": '[tool.pytest.ini_options]\nfilterwarnings = ["error"]\n',
                 "pyproject_markers": '[tool.pytest.ini_options]\nmarkers = ["slow: slow tests"]\n',
                 "pyproject_python_classes": '[tool.pytest.ini_options]\npython_classes = ["Check*"]\n',
                 "pyproject_other": "[tool.br_example]\nvalue = 1\n"}[detail]
        return c.modify("pyproject.toml", old, old + "\n" + extra)
    if detail.startswith("setup_cfg_"):
        old = ctx.text("setup.cfg")
        extra = {"setup_cfg_filterwarnings": "[tool:pytest]\nfilterwarnings =\n    error\n",
                 "setup_cfg_xfail_strict": "[tool:pytest]\nxfail_strict = true\n"}[detail]
        return c.modify("setup.cfg", old, old + "\n" + extra)
    if detail == "pytest_ini":
        return c.add("pytest.ini", rng.choice(["[pytest]\nxfail_strict = true\n", "[pytest]\nfilterwarnings =\n    error\n"]))
    if detail == "tox_ini":
        return c.add("tox.ini", rng.choice(["[pytest]\nfilterwarnings =\n    error\n", "[tox]\nenvlist = py313\n"]))
    if detail == "sitecustomize":
        loc = rng.choice(["sitecustomize.py", "tests/sitecustomize.py", "sqlglot/sitecustomize.py"])
        body = rng.choice(['import os\n\nos.environ.setdefault("BR_SITE", "1")\n',
                           "import sys\n\nsys.setrecursionlimit(200)\n"])
        return c.add(loc, body)
    raise ValueError(detail)


def fam_metadata(ctx, rng, detail):
    c = Cand(ctx, "metadata", detail)
    for _ in range(10):
        f = ctx.pick_src(rng)
        old = ctx.text(f)
        tree = ctx.tree(f)
        lines = _lines(old)
        if detail in ("docstring_text", "doctest_expect"):
            docs = [n.body[0] for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Module))
                    and n.body and _is_doc(n.body[0])]
            docs = [d for d in docs if ctx.ok_span(f, d.lineno, d.end_lineno)]
            if not docs:
                continue
            d = rng.choice(docs)
            if detail == "docstring_text":
                ln = rng.randint(d.lineno, d.end_lineno)
                line = lines[ln - 1]
                q = max(line.find('"""'), line.find("'''"))
                if ln == d.lineno and q >= 0:
                    lines[ln - 1] = line[:q + 3] + "Note. " + line[q + 3:]
                elif line.strip() and '"""' not in line and "'''" not in line and "\\" not in line:
                    lines[ln - 1] = line.rstrip("\n") + " (see notes)\n"
                else:
                    continue
            else:
                idx = [i for i in range(d.lineno, d.end_lineno - 1) if lines[i - 1].strip().startswith(">>>")
                       and lines[i].strip() and not lines[i].strip().startswith((">>>", "...", '"""'))]
                if not idx:
                    continue
                i = rng.choice(idx)
                lines[i] = lines[i].rstrip("\n") + " x\n"
            return c.modify(f, old, "".join(lines))
        if detail in ("annotation_param", "annotation_return"):
            funcs = [fn for fn, _ in _functions(tree)]
            anns = []
            for fn in funcs:
                if detail == "annotation_return":
                    if fn.returns is not None and fn.returns.lineno == fn.returns.end_lineno:
                        anns.append(fn.returns)
                else:
                    anns += [a.annotation for a in fn.args.args + fn.args.kwonlyargs
                             if a.annotation is not None and a.annotation.lineno == a.annotation.end_lineno]
            anns = [a for a in anns if ctx.ok(f, a.lineno)]
            if not anns:
                continue
            a = rng.choice(anns)
            cur = _seg(lines, a)
            alt = rng.choice([x for x in ("t.Any", "object", "int", "str | None", "BrMissing") if x != cur])
            return c.modify(f, old, _replace_node(old, a, alt))
        if detail == "annotation_attr":
            anns = [s.annotation for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef) for s in cls.body
                    if isinstance(s, ast.AnnAssign) and s.annotation.lineno == s.annotation.end_lineno]
            anns = [a for a in anns if ctx.ok(f, a.lineno)]
            if not anns:
                continue
            a = rng.choice(anns)
            cur = _seg(lines, a)
            alt = rng.choice([x for x in ("t.Any", "object", "int", "BrMissing", "t.ClassVar[int]") if x != cur])
            return c.modify(f, old, _replace_node(old, a, alt))
        if detail == "future_annotations":
            fut = [s for s in tree.body if isinstance(s, ast.ImportFrom) and s.module == "__future__"]
            if fut and ctx.ok(f, fut[0].lineno):
                s = fut[0]
                return c.modify(f, old, "".join(lines[:s.lineno - 1] + lines[s.lineno:]))
            if not fut and ctx.ok(f, 1):
                at = tree.body[0].end_lineno + 1 if tree.body and _is_doc(tree.body[0]) else 1
                return c.modify(f, old, _insert(old, at, ["from __future__ import annotations", ""], ""))
            continue
        if detail.startswith("line_shift"):
            k = rng.randint(1, 3)
            if detail == "line_shift_top":
                at = _top_import_end(tree) + 1
                block = [""] * k
                indent = ""
            else:
                f2, fn, _ = _pick_function(ctx, rng, files=[f])
                s = rng.choice(_real_body(fn))
                at = s.lineno
                indent = _stmt_indent(lines, s)
                block = [""] * k if detail == "line_shift_blank" else [rng.choice(COMMENTS[:-1])] * k
            if not ctx.ok(f, max(at - 1, 1)):
                continue
            return c.modify(f, old, _insert(old, at, block, indent), semantic=False)
        if detail == "source_text_comment":
            pool = [p for p in ctx.src if p.startswith("sqlglot/expressions/")]
            f = rng.choice(pool)
            old = ctx.text(f)
            lines = _lines(old)
            i = rng.randrange(len(lines))
            if not ctx.ok(f, i + 1):
                continue
            ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())] if lines[i].strip() else ""
            new = "".join(lines[:i] + [ind + "# br: note for readers of the source\n"] + lines[i:])
            if not _parses(new):
                continue
            return c.modify(f, old, new, semantic=False)
        if detail == "docs_text":
            f = rng.choice(ctx.docs)
            old = ctx.text(f)
            lines = _lines(old)
            i = rng.randrange(len(lines))
            if not ctx.ok(f, i + 1):
                continue
            return c.modify(f, old, "".join(lines[:i] + ["Additional note.\n"] + lines[i:]))
        raise ValueError(detail)
    raise Skip("no site")


def _token_flip_span(rng, src, ok_line, lo=1, hi=10 ** 9):
    """Like token_mutation, limited to lines [lo, hi] that ok_line accepts."""
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    lines = _lines(src)
    cands = []
    for i, t in enumerate(toks):
        if not (lo <= t.start[0] <= hi) or t.start[0] != t.end[0] or not ok_line(t.start[0]):
            continue
        if t.type == tokenize.OP and t.string in OPS:
            cands.append((t, OPS[t.string]))
        elif t.type == tokenize.NAME and t.string in NAMES:
            cands.append((t, NAMES[t.string]))
        elif t.type == tokenize.NUMBER and t.string.isdigit():
            n = int(t.string)
            cands.append((t, [str(n + 1)] + ([str(n - 1)] if n > 0 else [])))
        elif t.type == tokenize.STRING and len(t.string) < 30 and t.string[-1] in "'\"" \
                and not t.string.startswith(("f", "rb", "b", "\"\"\"", "'''", "r")):
            cands.append((t, [t.string[:-1] + "x" + t.string[-1]]))
    rng.shuffle(cands)
    for t, repl in cands[:40]:
        (sl, sc), (_, ec) = t.start, t.end
        line = lines[sl - 1]
        new = "".join(lines[:sl - 1] + [line[:sc] + rng.choice(repl) + line[ec:]] + lines[sl:])
        if _parses(new) and ast_differs(src, new):
            return new
    return None


def fam_registry(ctx, rng, detail):
    c = Cand(ctx, "registry", detail)
    for _ in range(12):
        f = ctx.pick_src(rng)
        old = ctx.text(f)
        tree = ctx.tree(f)
        lines = _lines(old)
        if detail == "class_rename":
            pool = [p for p in ctx.src if p.startswith(("sqlglot/expressions/", "sqlglot/dialects/"))]
            f = rng.choice(pool)
            old = ctx.text(f)
            classes = [n for n in ctx.tree(f).body if isinstance(n, ast.ClassDef) and ctx.ok(f, n.lineno)]
            if not classes:
                continue
            cls = rng.choice(classes)
            new = re.sub(rf"\b{cls.name}\b", cls.name + "Br", old)
            if any(not ctx.ok(f, r[0]) for r in _touched(old, new)):
                continue
            return c.modify(f, old, new)
        if detail in ("init_subclass_edit", "metaclass_edit", "dunder_edit"):
            fns = []
            for fn, cls in _functions(tree):
                if detail == "init_subclass_edit" and fn.name == "__init_subclass__":
                    fns.append(fn)
                elif detail == "metaclass_edit" and cls is not None and any(
                        isinstance(b, ast.Name) and b.id == "type" for b in cls.bases):
                    fns.append(fn)
                elif detail == "dunder_edit" and fn.name in ("__eq__", "__hash__", "__bool__", "__len__", "__ne__",
                                                             "__lt__", "__contains__", "__iter__", "__repr__"):
                    fns.append(fn)
            fns = [fn for fn in fns if _real_body(fn)]
            if not fns:
                continue
            fn = rng.choice(fns)
            body = _real_body(fn)
            if detail == "dunder_edit" and fn.name == "__eq__" and rng.random() < 0.4 and len(fn.args.args) > 1:
                o = fn.args.args[1].arg
                if not ctx.ok(f, body[0].lineno):
                    continue
                new = _insert(old, body[0].lineno, [f"if {o} is None:", "    return True"], _stmt_indent(lines, body[0]))
            else:
                new = _token_flip_span(rng, old, lambda ln: ctx.ok(f, ln), body[0].lineno, fn.end_lineno)
            if not new:
                continue
            return c.modify(f, old, new)
        if detail in ("decorator_arg", "decorator_remove"):
            decs = []
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                    decs += [d for d in n.decorator_list if d.lineno == d.end_lineno and ctx.ok(f, d.lineno)]
            if detail == "decorator_arg":
                consts = [a for d in decs if isinstance(d, ast.Call) for a in list(d.args) + [k.value for k in d.keywords]
                          if isinstance(a, ast.Constant)]
                if not consts:
                    continue
                a = rng.choice(consts)
                return c.modify(f, old, _replace_node(old, a, _flip_const(a.value)))
            decs = [d for d in decs if lines[d.lineno - 1].strip().startswith("@")]
            if not decs:
                continue
            d = rng.choice(decs)
            return c.modify(f, old, "".join(lines[:d.lineno - 1] + lines[d.lineno:]))
        if detail == "default_arg":
            defs = []
            for fn, _ in _functions(tree):
                defs += [d for d in fn.args.defaults + [k for k in fn.args.kw_defaults if k is not None]
                         if isinstance(d, ast.Constant) and d.lineno == d.end_lineno and ctx.ok(f, d.lineno)]
            if not defs:
                continue
            d = rng.choice(defs)
            return c.modify(f, old, _replace_node(old, d, _flip_const(d.value)))
        if detail.startswith("base_"):
            classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.bases and ctx.ok(f, n.lineno)
                       and all(b.lineno == n.lineno for b in n.bases)]
            if detail in ("base_remove", "base_swap"):
                classes = [n for n in classes if len(n.bases) >= 2]
            if not classes:
                continue
            cls = rng.choice(classes)
            bases = cls.bases
            if detail == "base_remove":
                i = rng.randrange(len(bases))
                b = bases[i]
                new = _remove_item(old, lines, b.lineno, b.col_offset, b.end_lineno, b.end_col_offset)
            elif detail == "base_swap":
                a, b = bases[0], bases[1]
                ta, tb = _seg(lines, a), _seg(lines, b)
                new = _replace(old, b.lineno, b.col_offset, b.end_col_offset, ta)
                new = _replace(new, a.lineno, a.col_offset, a.end_col_offset, tb)
            else:
                b = rng.choice(bases)
                cur = _seg(lines, b)
                others = sorted({_seg(lines, x) for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                                 for x in n.bases if x.lineno == x.end_lineno} - {cur, cls.name})
                others = [o for o in others if o not in ("object", "type") and not o.startswith(("t.", "Generic"))]
                if not others:
                    continue
                new = _replace_node(old, b, rng.choice(others))
            return c.modify(f, old, new)
        raise ValueError(detail)
    raise Skip("no site")


def _harmful_edit(ctx, rng, exclude, kind):
    """(path, old, new, desc) of an edit that is likely to break something."""
    for _ in range(15):
        if kind == "m0":
            f = rng.choice(ctx.dialect_code)
        else:
            f = ctx.pick_src(rng)
        if f in exclude:
            continue
        old = ctx.text(f)
        if kind == "m0":
            ss = [s for s in ctx.ops(f) if ctx.ok(f, s[1])]
            if not ss:
                continue
            new = _apply_site(old, rng.choice(ss))
        elif kind == "raise":
            try:
                _, fn, _ = _pick_function(ctx, rng, files=[f], tries=2)
            except Skip:
                continue
            s = _real_body(fn)[0]
            new = _insert(old, s.lineno, ['raise RuntimeError("br: injected")'], _stmt_indent(_lines(old), s))
        elif kind == "import":
            imports = [s for s in ctx.tree(f).body if isinstance(s, (ast.Import, ast.ImportFrom)) and len(s.names) == 1
                       and s.lineno == s.end_lineno and ctx.ok(f, s.lineno)
                       and not (isinstance(s, ast.ImportFrom) and s.module == "__future__")]
            if not imports:
                continue
            s = rng.choice(imports)
            lines = _lines(old)
            new = "".join(lines[:s.lineno - 1] + lines[s.lineno:])
        else:
            new = _token_flip_span(rng, old, lambda ln: ctx.ok(f, ln))
        if new and new != old and ast_differs(old, new):
            return f, old, new
    raise Skip("no harmful edit")


def _harmless_edit(ctx, rng, exclude, kind):
    for _ in range(15):
        if kind == "fixture_comment":
            f = rng.choice([x for x in ctx.fixtures if x.endswith(".sql")])
        else:
            f = ctx.pick_src(rng)
        if f in exclude:
            continue
        old = ctx.text(f)
        lines = _lines(old)
        i = rng.randrange(len(lines))
        if not ctx.ok(f, i + 1):
            continue
        if kind == "mode":
            if not ctx.ok_file(f):
                continue
            return f, old, None
        if kind == "docstring":
            docs = [n.body[0] for n in ast.walk(ctx.tree(f)) if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                    and n.body and _is_doc(n.body[0]) and ctx.ok(f, n.body[0].lineno)]
            if not docs:
                continue
            d = docs[rng.randrange(len(docs))]
            line = lines[d.lineno - 1]
            q = line.find('"""')
            if q < 0:
                continue
            lines[d.lineno - 1] = line[:q + 3] + "Note. " + line[q + 3:]
            return f, old, "".join(lines)
        if kind == "fixture_comment":
            return f, old, "".join(lines[:i] + ["-- note\n"] + lines[i:])
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())] if lines[i].strip() else ""
        add = [ind + rng.choice(COMMENTS[:-1]) + "\n"] if kind == "comment" else ["\n"]
        new = "".join(lines[:i] + add + lines[i:])
        if _parses(new) and not ast_differs(old, new):
            return f, old, new
    raise Skip("no harmless edit")


def fam_compound_mixed(ctx, rng, detail):
    """A harmless edit in one file plus a harmful one in another."""
    harmless, harmful = detail.split("+")
    c = Cand(ctx, "compound_mixed", detail)
    hf, hold, hnew = _harmless_edit(ctx, rng, set(), harmless)
    bf, bold, bnew = _harmful_edit(ctx, rng, {hf}, harmful)
    first = rng.random() < 0.5
    for f, old, new, sem in ([(hf, hold, hnew, False), (bf, bold, bnew, True)] if first
                             else [(bf, bold, bnew, True), (hf, hold, hnew, False)]):
        if new is None:
            c.mode(f)
        else:
            c.modify(f, old, new, semantic=sem)
    return c


def _func_index(ctx):
    idx = {}
    for f in ctx.src:
        for fn, _ in _functions(ctx.tree(f)):
            idx.setdefault(fn.name, []).append((f, fn))
    return idx


def _producer_edit(ctx, rng, f, fn):
    """Change what a function returns into a value of another type (or None into a fresh object)."""
    old = ctx.text(f)
    rets = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.lineno == n.end_lineno and ctx.ok(f, n.lineno)]
    if not rets:
        return None
    r = rng.choice(rets)
    lines = _lines(old)
    if r.value is None or (isinstance(r.value, ast.Constant) and r.value.value is None):
        text = "return " + rng.choice(['""', "0", "False", "[]"])
    else:
        seg = _seg(lines, r.value)
        text = "return " + rng.choice([f"({seg}) or None", f"str({seg})", f"[{seg}][0] if {seg} is not None else ''"])
    return _replace(old, r.lineno, r.col_offset, r.end_col_offset, text)


def fam_compound_eq(ctx, rng, detail):
    """An is<->== swap plus an edit to a type/equality producer in another file."""
    c = Cand(ctx, "compound_eq", detail)
    idx = ctx.cached("funcs", lambda: _func_index(ctx))
    for _ in range(20):
        f = ctx.pick_src(rng)
        old = ctx.text(f)
        tree = ctx.tree(f)
        sites = [s for s in ctx.cached(("swap", f), lambda: cmp_swap_sites(old, tree)) if ctx.ok(f, s[1])]
        if not sites:
            continue
        site = rng.choice(sites)
        swapped = _apply_site(old, site)
        if detail == "linked_return":
            # the compared operand is a name assigned from a call to a function defined elsewhere
            calls = []
            for fn, _ in _functions(tree):
                if not (fn.lineno <= site[1] <= fn.end_lineno):
                    continue
                for n in ast.walk(fn):
                    if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                        func = n.value.func
                        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                        if name and n.lineno < site[1]:
                            calls.append(name)
            targets = [(g, fn) for name in calls for g, fn in idx.get(name, []) if g != f]
            if not targets:
                continue
            g, gfn = rng.choice(targets)
            prod = _producer_edit(ctx, rng, g, gfn)
        elif detail == "dunder":
            pool = [(g, fn) for name in ("__eq__", "__hash__", "__ne__", "__bool__", "__len__")
                    for g, fn in idx.get(name, []) if g != f]
            if not pool:
                continue
            g, gfn = rng.choice(pool)
            gold = ctx.text(g)
            body = _real_body(gfn)
            if gfn.name == "__eq__" and len(gfn.args.args) > 1 and rng.random() < 0.5 and ctx.ok(g, body[0].lineno):
                prod = _insert(gold, body[0].lineno, [f"if {gfn.args.args[1].arg} is None:", "    return True"],
                               _stmt_indent(_lines(gold), body[0]))
            else:
                prod = _token_flip_span(rng, gold, lambda ln: ctx.ok(g, ln), body[0].lineno, gfn.end_lineno)
        else:  # any_return: an unrelated function's return type changes
            g = ctx.pick_src(rng)
            if g == f:
                continue
            fns = [fn for fn, _ in _functions(ctx.tree(g)) if any(isinstance(n, ast.Return) for n in ast.walk(fn))]
            if not fns:
                continue
            gfn = rng.choice(fns)
            prod = _producer_edit(ctx, rng, g, gfn)
        if not prod or not _parses(prod):
            continue
        c.modify(f, old, swapped)
        c.modify(g, ctx.text(g), prod)
        c.files[0]["role"] = "cmp_swap"
        c.files[1]["role"] = f"producer:{gfn.name}"
        return c
    raise Skip("no compound")


def fam_runtime_error(ctx, rng, detail):
    c = Cand(ctx, "runtime_error", detail)
    for _ in range(12):
        if detail in ("raise_entry_common", "raise_branch"):
            f = rng.choice(ctx.common)
        elif detail in ("raise_entry_rare", "crash_exit"):
            f = rng.choice(ctx.rare)
        else:
            f = ctx.pick_src(rng)
        old = ctx.text(f)
        lines = _lines(old)
        tree = ctx.tree(f)
        if detail in ("raise_entry_common", "raise_entry_rare", "crash_exit"):
            try:
                _, fn, _ = _pick_function(ctx, rng, files=[f], tries=3)
            except Skip:
                continue
            s = _real_body(fn)[0]
            block = ['raise RuntimeError("br: injected")'] if detail != "crash_exit" else ["import os", "", "os._exit(3)"]
            if detail == "crash_exit":
                c.tags.append("crash")
            return c.modify(f, old, _insert(old, s.lineno, block, _stmt_indent(lines, s)))
        if detail == "raise_branch":
            stmts = []
            for fn, _ in _functions(tree):
                for blk in _blocks(fn):
                    if blk is not fn.body:
                        stmts += [x for x in blk if ctx.ok(f, x.lineno) and x.lineno != fn.lineno]
            if not stmts:
                continue
            s = rng.choice(stmts)
            try:
                ind = _stmt_indent(lines, s)
            except Skip:
                continue
            return c.modify(f, old, _insert(old, s.lineno, ['raise RuntimeError("br: injected")'], ind))
        call = _call_time(tree)
        if detail in ("attr_rename", "method_typo"):
            nodes = [n for n in ast.walk(tree) if id(n) in call and isinstance(n, ast.Attribute)
                     and isinstance(n.ctx, ast.Load) and n.end_lineno == n.lineno and ctx.ok(f, n.lineno)]
            if detail == "method_typo":
                funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
                nodes = [n for n in nodes if id(n) in funcs]
            if not nodes:
                continue
            n = rng.choice(nodes)
            enc = lines[n.lineno - 1].encode("utf-8")
            end = n.end_col_offset
            if enc[end - len(n.attr):end].decode("utf-8", "replace") != n.attr:
                continue
            return c.modify(f, old, _replace(old, n.lineno, end, end, "_"))
        if detail == "name_typo":
            nodes = [n for n in ast.walk(tree) if id(n) in call and isinstance(n, ast.Name)
                     and isinstance(n.ctx, ast.Load) and ctx.ok(f, n.lineno) and n.id not in ("self", "cls")]
            if not nodes:
                continue
            n = rng.choice(nodes)
            return c.modify(f, old, _replace_node(old, n, n.id + "_"))
        raise ValueError(detail)
    raise Skip("no site")


def fam_timeout(ctx, rng, detail):
    c = Cand(ctx, "timeout", detail)
    c.tags.append("slow")
    pool = [f for f in ctx.dialect_code if any(f.endswith(f"/{d}.py") for d in RARE_DIALECTS)]
    f, fn, _ = _pick_function(ctx, rng, files=pool, tries=20)
    old = ctx.text(f)
    s = _real_body(fn)[0]
    block = {"while_true": ["while True:", "    pass"], "while_continue": ["while True:", "    continue"],
             "iter_forever": ["for _ in iter(int, 1):", "    pass"]}[detail]
    return c.modify(f, old, _insert(old, s.lineno, block, _stmt_indent(_lines(old), s)))


def _simple_edit(ctx, rng):
    """A one-token code edit somewhere in sqlglot (for malformed-diff carriers)."""
    for _ in range(10):
        f = ctx.pick_src(rng)
        old = ctx.text(f)
        new = _token_flip_span(rng, old, lambda ln: ctx.ok(f, ln))
        if new:
            return f, old, new
    raise Skip("no edit")


def _hunk_headers(text):
    return [m for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", text, re.M)]


def fam_malformed(ctx, rng, detail):
    c = Cand(ctx, "malformed", detail)
    if detail in ("empty", "whitespace_only", "header_only"):
        c.labellable = False
        c.regions.add(f"<{detail}>")
        c.parts.append({"empty": b"", "whitespace_only": b"\n",
                        "header_only": b"From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\n"
                                       b"From: Example <dev@example.invalid>\nSubject: [PATCH] Nothing to see\n\n---\n"
                                       b" 0 files changed\n"}[detail])
        if not c.ctx.ok(f"<{detail}>", 1, new=True):
            raise Skip("region")
        return c
    if detail in ("crlf_patch", "wrong_context", "bad_counts", "fuzzy_offset", "format_patch_header",
                  "plain_unified", "zero_context_modify"):
        f, old, new = _simple_edit(ctx, rng)
        c.modify(f, old, new)
        text = c.parts[0].decode("utf-8")
        if detail == "crlf_patch":
            c.labellable = False
            text = text.replace("\n", "\r\n")
        elif detail == "wrong_context":
            c.labellable = False
            body = text.split("\n")
            ctxl = [i for i, line in enumerate(body) if line.startswith(" ") and line.strip()]
            if not ctxl:
                raise Skip("no context")
            i = rng.choice(ctxl)
            body[i] = body[i] + "  # drifted"
            text = "\n".join(body)
        elif detail == "bad_counts":
            c.labellable = False
            m = _hunk_headers(text)[0]
            text = text[:m.start()] + f"@@ -{m.group(1)},{int(m.group(2) or 1) + 2} +{m.group(3)},{int(m.group(4) or 1) + 2} @@" \
                + text[m.end():]
        elif detail == "fuzzy_offset":
            k = rng.choice([-12, -5, -3, 3, 7, 25])
            m = _hunk_headers(text)[0]
            a, b = int(m.group(1)) + k, int(m.group(3)) + k
            if a < 1 or b < 1:
                raise Skip("offset")
            text = text[:m.start()] + f"@@ -{a},{m.group(2) or 1} +{b},{m.group(4) or 1} @@" + text[m.end():]
        elif detail == "format_patch_header":
            text = ("From 1111111111111111111111111111111111111111 Mon Sep 17 00:00:00 2001\n"
                    "From: Example Dev <dev@example.invalid>\nDate: Mon, 17 Nov 2025 10:00:00 +0000\n"
                    f"Subject: [PATCH] Tweak {os.path.basename(f)}\n\nSmall follow-up.\n---\n"
                    f" {f} | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n\n" + text + "-- \n2.43.0\n\n")
        elif detail == "plain_unified":
            text = text.split("\n", 1)[1]
        elif detail == "zero_context_modify":
            c.labellable = False
            body = "".join(_fix(x) for x in difflib.unified_diff(_lines(old), _lines(new), f"a/{f}", f"b/{f}", n=0))
            text = f"diff --git a/{f} b/{f}\n" + body
        c.parts = [text.encode("utf-8")]
        return c
    if detail == "zero_context_insert":
        f = ctx.pick_src(rng)
        old = ctx.text(f)
        lines = _lines(old)
        i = rng.randrange(1, len(lines))
        if not ctx.ok(f, i + 1):
            raise Skip("region")
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())] if lines[i].strip() else ""
        new = "".join(lines[:i] + [ind + "# Keep in sync with the generator\n"] + lines[i:])
        if not _parses(new):
            raise Skip("parse")
        c.modify(f, old, new, semantic=False)
        body = "".join(_fix(x) for x in difflib.unified_diff(_lines(old), _lines(new), f"a/{f}", f"b/{f}", n=0))
        c.parts = [(f"diff --git a/{f} b/{f}\n" + body).encode("utf-8")]
        return c
    if detail == "crlf_line":
        f, old, new = _simple_edit(ctx, rng)
        ln = _touched(old, new)[0][0]
        lines = _lines(new)
        lines[ln - 1] = lines[ln - 1].rstrip("\n") + "\r\n"
        return c.modify(f, old, "".join(lines))
    if detail == "no_eol_remove":
        f = rng.choice(ctx.src + [x for x in ctx.fixtures if x.endswith(".sql")])
        old = ctx.text(f)
        if not old.endswith("\n") or not ctx.ok(f, ctx.nlines(f) - 1):
            raise Skip("eol")
        return c.modify(f, old, old.rstrip("\n"), semantic=False)
    if detail == "no_eol_add":
        f = ctx.pick_src(rng)
        old = ctx.text(f)
        if not ctx.ok(f, ctx.nlines(f) - 1):
            raise Skip("region")
        return c.modify(f, old, old + rng.choice(["# end of module", "BR_TRAILING_VALUE = 1"]))
    if detail == "multi_hunk":
        f = ctx.pick_src(rng)
        old = ctx.text(f)
        a = _token_flip_span(rng, old, lambda ln: ctx.ok(f, ln))
        if not a:
            raise Skip("edit")
        first = _touched(old, a)[0][0]
        lines = _lines(a)
        far = [i for i in range(len(lines)) if abs(i + 1 - first) > 20 and lines[i].strip() and ctx.ok(f, i + 1)]
        if not far:
            raise Skip("short file")
        i = rng.choice(far)
        ind = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
        new = "".join(lines[:i] + [ind + rng.choice(COMMENTS[:-1]) + "\n"] + lines[i:])
        if not _parses(new):
            raise Skip("parse")
        return c.modify(f, old, new)
    if detail == "dup_stanza":
        f = ctx.pick_src(rng)
        old = ctx.text(f)
        a = _token_flip_span(rng, old, lambda ln: ctx.ok(f, ln))
        if not a:
            raise Skip("edit")
        first = _touched(old, a)[0][0]
        b = _token_flip_span(rng, a, lambda ln: ctx.ok(f, ln) and ln > first + 10)
        if not b:
            raise Skip("edit")
        c.modify(f, old, a)
        c.modify(f, a, b)
        return c
    if detail == "latin1_cookie":
        pool = [f for f in ctx.src if any(ch > 127 for ch in ctx.data(f))]
        f = rng.choice(pool)
        old = ctx.text(f)
        if not ctx.ok(f, 1):
            raise Skip("region")
        return c.modify(f, old, "# -*- coding: latin-1 -*-\n" + old, semantic=False)
    if detail == "latin1_new_file":
        w = rng.choice(WORDS)
        path = f"sqlglot/br_{w}_latin1.py"
        content = "# -*- coding: latin-1 -*-\nNAME = \"caf\xe9\"\n".encode("latin-1")
        raw = (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,2 @@\n"
               .encode() + b"".join(b"+" + line + b"\n" for line in content.splitlines()))
        return c.add(path, None, raw=raw)
    if detail == "unicode_edit":
        for _ in range(10):
            f = ctx.pick_src(rng, [x for x in ctx.src if any(ch > 127 for ch in ctx.data(x))])
            old = ctx.text(f)
            consts = [n for n in ast.walk(ctx.tree(f)) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                      and any(ord(ch) > 127 for ch in n.value) and n.lineno == n.end_lineno and ctx.ok(f, n.lineno)]
            if consts:
                n = rng.choice(consts)
                return c.modify(f, old, _replace_node(old, n, repr(n.value + "é")))
        raise Skip("no unicode")
    if detail == "binary_gz":
        f = rng.choice([x for x in ctx.gz_small if ctx.ok_file(x)])
        import gzip

        rows = gzip.decompress(ctx.data(f)).split(b"\n")
        i = rng.randrange(len(rows) - 1) if len(rows) > 1 else 0
        rows[i] = rows[i].replace(b"|", b"|9", 1) if b"|" in rows[i] else rows[i] + b"9"
        return c.binary(f, _gzip_stored(b"\n".join(rows)))
    if detail == "binary_new":
        w = rng.choice(WORDS)
        blob = bytes(range(256)) + w.encode()
        return c.add(f"tests/fixtures/br_{w}.bin", None, raw=binary_diff(f"tests/fixtures/br_{w}.bin", None, blob))
    raise ValueError(detail)


def fam_shared_state(ctx, rng, detail):
    """State that leaks between tests: a test can pass alone yet fail in suite order."""
    c = Cand(ctx, "shared_state", detail)
    for _ in range(15):
        if detail == "dispatch_cache":
            f = "sqlglot/generator.py"
            old = ctx.text(f)
            target = "        dispatch = _DISPATCH_CACHE.get(cls)\n"
            ln = old[:old.index(target)].count("\n") + 1
            if not ctx.ok(f, ln):
                raise Skip("region")
            alt = rng.choice(["        dispatch = _DISPATCH_CACHE.get(cls.__mro__[1]) or _DISPATCH_CACHE.get(cls)\n",
                              "        dispatch = next(iter(_DISPATCH_CACHE.values()), None) if len(_DISPATCH_CACHE) > 40 "
                              "else _DISPATCH_CACHE.get(cls)\n"])
            return c.modify(f, old, old.replace(target, alt, 1))
        f = ctx.pick_src(rng, ctx.common if detail == "counter_degrade" and rng.random() < 0.5 else None)
        if detail == "counter_degrade" and rng.random() < 0.1:
            f = "tests/helpers.py"
        old = ctx.text(f)
        tree = ctx.tree(f)
        lines = _lines(old)
        if detail == "counter_degrade":
            try:
                _, fn, _ = _pick_function(ctx, rng, files=[f], tries=2)
            except Skip:
                continue
            end = _top_import_end(tree)
            if not end or not ctx.ok(f, end):
                continue
            s = _real_body(fn)[0]
            limit = rng.choice([50, 500, 5000])
            ind = _stmt_indent(lines, s)
            ret = "return None" if not any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(fn)) else "return"
            new = _insert(old, s.lineno, ["_BR_CALLS[0] += 1", f"if _BR_CALLS[0] > {limit}:", f"    {ret}"], ind)
            new = _insert(new, end + 1, ["", "_BR_CALLS = [0]"], "")
            c.detail = f"{detail}:{limit}"
            return c.modify(f, old, new)
        if detail in ("class_table_mutate", "module_table_mutate"):
            for owner, s, v, n in rng.sample(_tables(ctx, f), min(6, len(_tables(ctx, f)))):
                is_cls = isinstance(owner, ast.ClassDef)
                if is_cls != (detail == "class_table_mutate"):
                    continue
                tgt = s.targets[0] if isinstance(s, ast.Assign) else s.target
                if not isinstance(tgt, ast.Name) or not isinstance(v, (ast.Dict, ast.Set)):
                    continue
                if isinstance(v, ast.Dict):
                    keys = [k for k in v.keys if k is not None and k.lineno == k.end_lineno]
                    if not keys:
                        continue
                    mut = f".pop({_seg(lines, rng.choice(keys))}, None)"
                else:
                    elts = [e for e in v.elts if e.lineno == e.end_lineno]
                    if not elts:
                        continue
                    mut = f".discard({_seg(lines, rng.choice(elts))})"
                if is_cls:
                    meths = [fn for fn in owner.body if isinstance(fn, ast.FunctionDef) and fn.args.args
                             and fn.args.args[0].arg == "self" and _real_body(fn)]
                    stmt = f"self.{tgt.id}{mut}"
                else:
                    meths = [fn for fn, _ in _functions(tree) if _real_body(fn)
                             and any(isinstance(x, ast.Name) and x.id == tgt.id for x in ast.walk(fn))]
                    stmt = f"{tgt.id}{mut}"
                meths = [m for m in meths if ctx.ok(f, _real_body(m)[0].lineno)]
                if not meths:
                    continue
                fn = rng.choice(meths)
                b0 = _real_body(fn)[0]
                return c.modify(f, old, _insert(old, b0.lineno, [stmt], _stmt_indent(lines, b0)))
            continue
        if detail == "add_lru_cache":
            fns = [fn for fn in tree.body if isinstance(fn, ast.FunctionDef) and _real_body(fn)
                   and not fn.decorator_list and not any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(fn))
                   and ctx.ok(f, fn.lineno)]
            end = _top_import_end(tree)
            if not fns or not end or not ctx.ok(f, end):
                continue
            fn = rng.choice(fns)
            new = _insert(old, fn.lineno, ["@functools.lru_cache(maxsize=None)"], "")
            if not re.search(r"^import functools$", old, re.M):
                new = _insert(new, end + 1, ["import functools"], "")
            return c.modify(f, old, new)
        raise ValueError(detail)
    raise Skip("no site")


def fam_test_edit(ctx, rng, detail):
    c = Cand(ctx, "test_edit", detail)
    for _ in range(12):
        if detail in ("test_token", "test_skip", "test_rename", "test_collect_error", "test_syntax"):
            f = rng.choice(ctx.test_modules)
        elif detail.startswith("helpers_"):
            f = "tests/helpers.py"
        if detail == "test_token" or detail == "helpers_token":
            old = ctx.text(f)
            new = _token_flip_span(rng, old, lambda ln: ctx.ok(f, ln))
            if not new:
                continue
            return c.modify(f, old, new)
        if detail in ("test_skip", "test_rename"):
            old = ctx.text(f)
            lines = _lines(old)
            fns = [fn for fn, cls in _functions(ctx.tree(f)) if cls is not None and fn.name.startswith("test")
                   and _real_body(fn) and ctx.ok(f, fn.lineno)]
            if not fns:
                continue
            fn = rng.choice(fns)
            if detail == "test_skip":
                s = _real_body(fn)[0]
                new = _insert(old, s.lineno, ['self.skipTest("br: temporarily disabled")'], _stmt_indent(lines, s))
            else:
                line = lines[fn.lineno - 1]
                if f"def {fn.name}(" not in line:
                    continue
                new = old.replace(line, line.replace(f"def {fn.name}(", f"def {fn.name}_br(", 1), 1)
            return c.modify(f, old, new)
        if detail == "test_collect_error":
            old = ctx.text(f)
            end = _top_import_end(ctx.tree(f))
            if not end or not ctx.ok(f, end):
                continue
            return c.modify(f, old, _insert(old, end + 1, ["import sqlglot_br_missing  # noqa: F401"], ""))
        if detail == "test_syntax":
            old = ctx.text(f)
            new = syntax_mutation(rng, old)
            if not new or not all(ctx.ok(f, r[0]) for r in _touched(old, new)):
                continue
            return c.modify(f, old, new, syntax_error=True)
        if detail == "helpers_delete_line":
            old = ctx.text(f)
            new = delete_line_mutation(rng, old)
            if not new or not all(ctx.ok(f, r[0]) for r in _touched(old, new)):
                continue
            return c.modify(f, old, new)
        if detail == "fixture_sql":
            f = rng.choice([x for x in ctx.fixtures if x.endswith(".sql")])
            old = ctx.text(f)
            new = fixture_mutation(rng, old)
            if not new or new == old or not all(ctx.ok(f, r[0]) for r in _touched(old, new)):
                continue
            return c.modify(f, old, new)
        if detail == "fixture_json":
            f = "tests/fixtures/jsonpath/cts.json"
            old = ctx.text(f)
            lines = _lines(old)
            idx = [i for i, line in enumerate(lines) if ('"' in line or "true" in line or "false" in line) and ctx.ok(f, i + 1)]
            if not idx:
                raise Skip("region")
            i = rng.choice(idx)
            line = lines[i]
            if "true" in line or "false" in line:
                lines[i] = line.replace("true", "false", 1) if "true" in line else line.replace("false", "true", 1)
            else:
                j = line.rfind('"')
                lines[i] = line[:j] + "x" + line[j:]
            return c.modify(f, old, "".join(lines))
        if detail == "fixture_gz":
            return _retag(fam_malformed(ctx, rng, "binary_gz"), c)
        if detail == "fixture_license":
            f = "tests/fixtures/jsonpath/LICENSE"
            old = ctx.text(f)
            return c.modify(f, old, old + "\nSee the upstream repository for details.\n")
        if detail == "fixture_tpc_sql":
            pool = [x for x in ctx.fixtures if "/tpc-" in x and x.endswith(".sql")]
            f = rng.choice(pool)
            old = ctx.text(f)
            new = fixture_mutation(rng, old)
            if not new or new == old or not all(ctx.ok(f, r[0]) for r in _touched(old, new)):
                continue
            return c.modify(f, old, new)
        raise ValueError(detail)
    raise Skip("no site")


def _retag(src, dst):
    dst.parts, dst.files, dst.regions, dst.labellable = src.parts, src.files, src.regions, src.labellable
    return dst


def fam_legacy(name):
    """The pre-v2 kinds, region-filtered."""
    def gen(ctx, rng, detail):
        c = Cand(ctx, name, detail)
        if name == "docs_file":
            f = rng.choice(ctx.docs)
            old = ctx.text(f)
            return c.modify(f, old, old + "\nMore notes.\n")
        f = ctx.pick_src(rng)
        if name == "comment" and rng.random() < 0.2:
            f = rng.choice(ctx.tests_py + ctx.fixtures)
        old = ctx.text(f)
        if name == "token_flip":
            new = token_mutation(rng, old)
        elif name == "class_literal":
            new = class_literal_mutation(rng, old)
        elif name == "cmp_swap":
            new = cmp_swap_mutation(rng, old)
        elif name == "comment":
            new = comment_mutation(rng, old, inline=detail == "inline")
        elif name == "delete_line":
            new = delete_line_mutation(rng, old)
        elif name == "syntax_error":
            new = syntax_mutation(rng, old)
        elif name == "hash_order":
            tree = ctx.tree(f)
            call = _call_time(tree)
            sites = [n for n in ast.walk(tree) if id(n) in call and isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "sorted" and len(n.args) == 1
                     and not n.keywords and ctx.ok(f, n.lineno)]
            if not sites:
                raise Skip("no sorted")
            n = rng.choice(sites)
            new = _replace_node(old, n.func, "list")
        else:
            raise ValueError(name)
        if not new or new == old:
            raise Skip("no change")
        if not all(ctx.ok(f, r[0]) for r in _touched(old, new)):
            raise Skip("region")
        return c.modify(f, old, new, semantic=name not in ("comment", "syntax_error"),
                        syntax_error=name == "syntax_error")
    return gen


# (family, count per set, generator, details cycled in a per-set shuffled order)
FAMILIES = [
    ("m0_op", 135, fam_m0_op, ["lt", "lt", "eq", "eq", "is", "is", "copy", "copy", "boolop", "not", "kwbool", "in"]),
    ("insert_branch", 20, fam_insert_branch, ["entry_return", "entry_raise", "middle_return", "after_return_dead",
                                             "after_if_return", "loop_continue", "loop_break", "early_stmt"]),
    ("class_table", 22, fam_class_table, ["dict_remove", "dict_add", "dict_value", "unpack_remove", "set_remove",
                                         "set_add", "seq_remove", "seq_add", "bool_flip", "num", "str",
                                         "large_dict_remove", "large_dict_add", "large_dict_value", "large_set_remove",
                                         "large_set_add"]),
    ("import_edit", 12, fam_import_edit, ["remove_used", "remove_name", "alter_name", "alter_module", "missing_module",
                                         "guarded_missing", "cycle", "lazy_missing", "typing_only"]),
    ("file_ops", 18, fam_file_ops, ["shadow_pkg_empty", "shadow_pkg_copy", "new_test_pass", "new_test_fail",
                                   "new_test_collect_error", "new_top_module", "shadow_thirdparty", "new_sqlglot_module",
                                   "new_sqlglot_module_raises", "new_dialect_module", "new_fixture", "delete_module",
                                   "delete_test", "delete_fixture", "delete_fixture_gz", "rename_module",
                                   "rename_fixture", "rename_edit", "mode_exec", "mode_with_edit"]),
    ("hook_config", 10, fam_hook_config, list(CONFTESTS) + ["pyproject_filterwarnings", "pyproject_markers",
                                                           "pyproject_python_classes", "pyproject_other",
                                                           "setup_cfg_filterwarnings", "setup_cfg_xfail_strict",
                                                           "pytest_ini", "tox_ini", "sitecustomize"]),
    ("metadata", 16, fam_metadata, ["docstring_text", "doctest_expect", "annotation_param", "annotation_return",
                                   "annotation_attr", "future_annotations", "line_shift_top", "line_shift_blank",
                                   "line_shift_comment", "source_text_comment", "docs_text"]),
    ("registry", 14, fam_registry, ["class_rename", "init_subclass_edit", "metaclass_edit", "dunder_edit",
                                   "decorator_arg", "decorator_remove", "default_arg", "base_remove", "base_swap",
                                   "base_replace"]),
    ("compound_mixed", 8, fam_compound_mixed, ["comment+m0", "docstring+raise", "mode+token", "blank+import",
                                              "fixture_comment+m0", "comment+token", "blank+raise", "docstring+m0"]),
    ("runtime_error", 12, fam_runtime_error, ["raise_entry_common", "raise_entry_rare", "raise_branch", "attr_rename",
                                             "method_typo", "name_typo", "raise_entry_common", "raise_entry_rare",
                                             "raise_branch", "attr_rename", "name_typo", "crash_exit"]),
    ("timeout", 3, fam_timeout, ["while_true", "while_continue", "iter_forever"]),
    ("malformed", 22, fam_malformed, ["crlf_patch", "crlf_line", "no_eol_remove", "no_eol_add", "zero_context_insert",
                                     "zero_context_modify", "multi_hunk", "dup_stanza", "latin1_cookie",
                                     "latin1_new_file", "unicode_edit", "binary_gz", "binary_new", "empty",
                                     "whitespace_only", "header_only", "wrong_context", "bad_counts", "fuzzy_offset",
                                     "format_patch_header", "plain_unified"]),
    ("test_edit", 16, fam_test_edit, ["test_token", "test_skip", "test_rename", "test_collect_error", "test_syntax",
                                     "helpers_token", "helpers_delete_line", "fixture_sql", "fixture_json",
                                     "fixture_gz", "fixture_license", "fixture_tpc_sql", "test_token", "fixture_sql"]),
    ("token_flip", 6, fam_legacy("token_flip"), ["-"]),
    ("class_literal", 4, fam_legacy("class_literal"), ["-"]),
    ("cmp_swap", 4, fam_legacy("cmp_swap"), ["-"]),
    ("comment", 4, fam_legacy("comment"), ["line", "inline"]),
    ("delete_line", 3, fam_legacy("delete_line"), ["-"]),
    ("syntax_error", 2, fam_legacy("syntax_error"), ["-"]),
    ("docs_file", 1, fam_legacy("docs_file"), ["-"]),
    ("hash_order", 3, fam_legacy("hash_order"), ["sorted->list"]),
    # holdout only
    ("literal_init_error", 14, fam_literal_init_error, ["neg_str", "unhashable_dict", "tuple_list_key",
                                                       "unhashable_set", "neg_tuple", "key_to_list", "member_to_list"]),
    ("shared_state", 14, fam_shared_state, ["counter_degrade", "class_table_mutate", "module_table_mutate",
                                           "add_lru_cache", "dispatch_cache", "counter_degrade", "class_table_mutate"]),
    ("compound_eq", 10, fam_compound_eq, ["linked_return", "dunder", "any_return", "linked_return", "dunder"]),
]
GEN_ERRORS = (Skip, SyntaxError, ValueError, IndexError, KeyError, UnicodeError, tokenize.TokenError,
              RecursionError, AttributeError, TypeError, StopIteration)


def source_digest(repo):
    """sha256 over the files the generator can read (sources, tests, fixtures, root config)."""
    h = hashlib.sha256()
    paths = rel_files(repo, "sqlglot", ("",)) + rel_files(repo, "tests", ("",))
    paths += [p for p in ("pyproject.toml", "setup.cfg") if os.path.exists(os.path.join(repo, p))] + _docs(repo)
    for p in sorted(p for p in paths if not p.endswith((".pyc", ".so")) and "/." not in p):
        with open(os.path.join(repo, p), "rb") as fh:
            h.update(p.encode() + b"\0" + hashlib.sha256(fh.read()).digest())
    return h.hexdigest()


_DEBUG_SEEN = set()


def build_set(repo, split, seed, log=None):
    """Generate one set. Returns (patches, families): patches is a list of dicts with bytes."""
    ctx = Ctx(repo, split)
    made = []
    seen = set()
    for family, count, fn, details in FAMILIES:
        if split == "dev" and family in HOLDOUT_ONLY:
            continue
        t_fam = time.monotonic()
        order = list(details)
        random.Random(f"{GEN_VERSION}|details|{seed}|{split}|{family}").shuffle(order)
        for i in range(count):
            for attempt in range(400):
                detail = order[(i + attempt // 40) % len(order)]
                rng = random.Random(f"{GEN_VERSION}|{seed}|{split}|{family}|{i}|{attempt}")
                try:
                    cand = fn(ctx, rng, detail)
                except GEN_ERRORS:
                    if os.environ.get("BR_MUTANTS_DEBUG") and not isinstance(sys.exc_info()[1], Skip):
                        import traceback

                        where = traceback.extract_tb(sys.exc_info()[2])[-1]
                        if (where.lineno, family) not in _DEBUG_SEEN:
                            _DEBUG_SEEN.add((where.lineno, family))
                            print(f"debug {family}/{detail}: {sys.exc_info()[1]!r} at line {where.lineno}", file=sys.stderr)
                    continue
                if cand is None or not cand.regions or any(split_of(r) != split for r in cand.regions):
                    continue
                data = cand.data()
                h = _sha(data)
                if h in seen:
                    continue
                seen.add(h)
                made.append({"family": family, "detail": cand.detail, "files": cand.files,
                             "regions": sorted(cand.regions), "labellable": cand.labellable,
                             "tags": cand.tags, "data": data, "sha256": h})
                break
            else:
                raise RuntimeError(f"{split}: could not generate {family} #{i}")
        if log:
            log(f"{split}: {family} x{count} ({time.monotonic() - t_fam:.1f}s)")
    random.Random(f"{GEN_VERSION}|ids|{seed}|{split}").shuffle(made)
    for k, p in enumerate(made, 1):
        p["id"] = f"{ID_PREFIX[split]}{k:03d}"
    made.sort(key=lambda p: p["id"])
    return made


def set_digest(patches):
    return _sha("".join(f"{p['id']}\t{p['sha256']}\n" for p in patches).encode())


def make_manifest(repo, split, seed, patches):
    fams = {}
    for p in patches:
        fams[p["family"]] = fams.get(p["family"], 0) + 1
    return {
        "generator": "tools/mutants.py", "generator_version": GEN_VERSION, "split": split, "seed": seed,
        "pinned_commit": PINNED_COMMIT, "source_digest": source_digest(repo),
        "python": "%d.%d" % sys.version_info[:2],
        "split_rule": {"salt": SPLIT_SALT, "huge_lines": HUGE_LINES, "bucket_lines": BUCKET_LINES,
                       "bucketed_prefixes": ["sqlglot/expressions/"], "bucket_overrides": BUCKET_OVERRIDES,
                       "holdout_only_families": list(HOLDOUT_ONLY),
                       "rule": "region = path, or path#((line-1)//bucket) for files >= huge_lines lines, under "
                               "bucketed_prefixes or in bucket_overrides; new files: their own path. "
                               "dev iff int(sha256(salt+region)[:8], 16) % 2 == 0. Every region a patch touches "
                               "is in its set."},
        "count": len(patches), "families": dict(sorted(fams.items())),
        "unlabellable": sorted(p["id"] for p in patches if not p["labellable"]),
        "set_sha256": set_digest(patches),
        "patches": [{k: p[k] for k in ("id", "family", "detail", "files", "regions", "labellable", "tags", "sha256")}
                    | {"bytes": len(p["data"])} for p in patches],
    }


def check_apply(repo, patches, scratch):
    """`git apply --check` every patch against a scratch copy of the pinned source, and apply each
    to a copy of just the files it touches to confirm it changes something. Returns problems."""
    import shutil
    import subprocess

    scratch = os.path.abspath(scratch)
    src = os.path.join(scratch, "src")
    if not os.path.isdir(src):
        shutil.copytree(repo, src, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    # Outside any repository: inside one, git apply skips paths silently and exits 0.
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=scratch)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(var, None)
    problems = []
    for p in patches:
        pf = os.path.join(scratch, p["id"] + ".patch")
        with open(pf, "wb") as fh:
            fh.write(p["data"])
        full = subprocess.run(["git", "apply", "--check", pf], cwd=src, env=env, capture_output=True)
        ok = full.returncode == 0
        if ok != p["labellable"]:
            problems.append(f"{p['id']} ({p['family']}/{p['detail']}): git apply --check "
                            f"{'accepts' if ok else 'rejects'} it, manifest says labellable={p['labellable']}: "
                            + full.stderr.decode(errors="replace")[-200:].strip())
        if ok:
            mini = os.path.join(scratch, "mini-" + p["id"])
            shutil.rmtree(mini, ignore_errors=True)
            os.makedirs(mini)
            touched = []
            for f in p["files"]:
                touched += [f["path"]] + ([f["to"]] if "to" in f else [])
            before = {}
            for rel in touched:
                srcf = os.path.join(src, rel)
                if os.path.isfile(srcf):
                    os.makedirs(os.path.dirname(os.path.join(mini, rel)), exist_ok=True)
                    shutil.copy2(srcf, os.path.join(mini, rel))
                    before[rel] = (open(srcf, "rb").read(), os.stat(srcf).st_mode)
            res = subprocess.run(["git", "apply", pf], cwd=mini, env=env, capture_output=True)
            after = {}
            for rel in touched:
                fp = os.path.join(mini, rel)
                if os.path.isfile(fp):
                    after[rel] = (open(fp, "rb").read(), os.stat(fp).st_mode)
            if res.returncode != 0:
                problems.append(f"{p['id']}: applies to the tree but not to its touched files: "
                                + res.stderr.decode(errors="replace")[-200:].strip())
            elif before == after:
                problems.append(f"{p['id']} ({p['family']}/{p['detail']}): applying it changes nothing")
            shutil.rmtree(mini, ignore_errors=True)
        os.remove(pf)
    return problems


def _write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def v2_main(args):
    def log(msg):
        print(msg, file=sys.stderr)

    if args.verify_manifest:
        with open(args.verify_manifest) as fh:
            man = json.load(fh)
        problems = []
        if man.get("generator_version") != GEN_VERSION:
            problems.append(f"manifest is {man.get('generator_version')}, generator is {GEN_VERSION}")
        py = "%d.%d" % sys.version_info[:2]
        if man.get("python") != py:
            log(f"warning: manifest built with Python {man.get('python')}, running {py} (ast/tokenize may differ)")
        digest = source_digest(args.repo)
        if digest != man.get("source_digest"):
            problems.append("source tree differs from the one the manifest was built from")
        patches = build_set(args.repo, man["split"], man["seed"])
        mine = {p["id"]: p for p in patches}
        theirs = {p["id"]: p for p in man["patches"]}
        for pid in sorted(set(mine) | set(theirs)):
            if pid not in mine or pid not in theirs or mine[pid]["sha256"] != theirs[pid]["sha256"]:
                problems.append(f"{pid}: regenerated patch differs")
        if set_digest(patches) != man["set_sha256"]:
            problems.append("set sha256 differs")
        if args.check_apply:
            problems += check_apply(args.repo, patches, args.check_apply)
        if args.out:
            on_disk = sorted(p for p in os.listdir(args.out) if p.endswith(".patch"))
            if on_disk != sorted(pid + ".patch" for pid in mine):
                problems.append(f"{args.out}: file list differs from the manifest")
            for pid, p in sorted(mine.items()):
                fp = os.path.join(args.out, pid + ".patch")
                if os.path.exists(fp) and open(fp, "rb").read() != p["data"]:
                    problems.append(f"{fp}: bytes differ")
        for msg in problems[:50]:
            print("MISMATCH " + msg)
        if problems:
            print(f"verify FAILED: {len(problems)} problem(s)")
            return 1
        print(f"verify OK: {man['split']} set, {len(patches)} patches, set sha256 {man['set_sha256']}")
        return 0

    seed = DEFAULT_SEEDS[args.split] if args.seed is None else args.seed
    patches = build_set(args.repo, args.split, seed, log=log)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        names = {p["id"] + ".patch" for p in patches}
        stale = sorted(p for p in os.listdir(args.out) if p.endswith(".patch") and p not in names)
        if stale and not args.clean:
            log(f"{args.out} holds {len(stale)} other .patch files (e.g. {stale[0]}); pass --clean to remove them")
            return 2
        for name in stale:
            os.remove(os.path.join(args.out, name))
        for p in patches:
            _write_atomic(os.path.join(args.out, p["id"] + ".patch"), p["data"])
    if args.check_apply:
        problems = check_apply(args.repo, patches, args.check_apply)
        for msg in problems:
            print("APPLY " + msg)
        if problems:
            return 1
        print(f"apply check OK: {len(patches)} patches")
    man = make_manifest(args.repo, args.split, seed, patches)
    if args.manifest:
        _write_atomic(args.manifest, (json.dumps(man, indent=1, sort_keys=True) + "\n").encode())
    fams = ", ".join(f"{k} {v}" for k, v in man["families"].items())
    print(f"{args.split}: {len(patches)} patches ({len(man['unlabellable'])} unlabellable), "
          f"set sha256 {man['set_sha256']}\n  {fams}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--kinds", default=None, help="comma separated subset")
    ap.add_argument("--split", choices=["dev", "holdout"], default=None,
                    help="v2 corpus: emit the development or the frozen holdout set")
    ap.add_argument("--manifest", default=None, help="v2 corpus: write the set's manifest here")
    ap.add_argument("--verify-manifest", default=None,
                    help="v2 corpus: regenerate the set a manifest describes and check it byte for byte")
    ap.add_argument("--clean", action="store_true", help="v2 corpus: remove other *.patch files in --out")
    ap.add_argument("--check-apply", default=None, metavar="SCRATCH_DIR",
                    help="v2 corpus: git apply --check every patch against a copy of --repo made in this "
                         "directory and confirm the labellable flags and that each patch changes something")
    args = ap.parse_args()
    if args.split or args.verify_manifest:
        sys.exit(v2_main(args))
    if not args.out:
        ap.error("--out is required")
    if args.seed is None:
        args.seed = 0
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
