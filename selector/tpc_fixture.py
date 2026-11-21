"""BR_TPC_FIXTURE=1: which TestExecutor tests a plain edit of a TPC fixture file can affect.

The maps record every test of tests/test_executor.py::TestExecutor as a reader of both
tests/fixtures/optimizer/tpc-h/tpc-h.sql and .../tpc-ds/tpc-ds.sql, because the class's
setUpClass loads both. Reading the pinned tests (hashes below; tests/test_tpc_fixture.py checks
these facts against the pinned file's AST):

  tests/helpers.py
    11-12  _filter_comments: str.splitlines, drops empty lines and lines starting with "--"
    15-24  _extract_meta: leading "#" lines become meta (ValueError without ":", IndexError
           when every line starts with "#")
    44-55  load_sql_fixture_pairs: open(..., encoding="utf-8") (universal newlines), split on
           ";", yields (meta, sql, expected) per pair, ignores an odd last statement
  tests/test_executor.py
    118-119  setUpClass: cls.tpch_sqls / cls.tpcds_sqls = list(load_sql_fixture_pairs(...))
    126-130  cached_execute: cls.cache keyed by the sql text alone, shared by tpc-h and tpc-ds
    157-163  test_optimized_tpch: tpch_sqls as (_, sql, optimized), cached_execute(sql) and
             tpch_conn.execute(optimized): consumes (sql, expected), never meta
    165-173  subtestHelper: tpch_sqls[i] / tpcds_sqls[i] as (_, sql, _), cached_execute(sql)
    175-185  _mp_execute: `for args, sql, _ in sqls`: parse_one(sql), mp_execute(..., meta) in
             a spawned pool; subtestHelper(i) only when the child returns a table, which
             happens only when meta.get("execute") is truthy (mp_execute, 57-67)
    187-191  test_execute_tpch / test_execute_tpcds: _mp_execute over tpch_sqls / tpcds_sqls
  No other function or test of the module touches tpch_sqls, tpcds_sqls, cache,
  cached_execute, subtestHelper, _mp_execute, tpch_conn or tpcds_conn, and nothing outside
  tests/test_executor.py references TestExecutor or these attributes. tests/__init__.py is empty.

So the other 57 tests depend on the fixture only through setUpClass succeeding, which is
decided by the loader alone (the csv.gz tables are other files). test_execute_tpcds depends on
the sequence of (meta, sql) of tpc-ds; test_execute_tpch on the sequence of (meta, sql) of
tpc-h; test_optimized_tpch on the sequence of (sql, expected) of tpc-h. Shared state between
them: the cache (keyed by sql text: a tpc-h sql equal to a tpc-ds sql would hand one suite the
other's result, so such a collision falls back), and tpch_conn, on which test_optimized_tpch
executes the expected statements and which test_execute_tpch reads only through
cached_execute, that is only when some tpc-h meta has a truthy "execute".

Anything not exactly like this (other hashes, other files of the commit that could change the
loader, the test module or its environment, a patch git apply could place elsewhere, a loader
error, an oversized file) returns a reason instead, and the caller keeps every reader.
"""

import hashlib
import posixpath
import re

from .patch import apply, misplaced_hunk

TPCH = "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
TPCDS = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
FIXTURES = (TPCH, TPCDS)

PINNED = {
    "tests/helpers.py": "c0dc285fccd1b8bee4a451f5962118fe771de005843b56e3af4c7136f74c3ed3",
    "tests/test_executor.py": "270a844a7b0b84045efde017135e6208ca07d7d93eba395be17c20c6e918923d",
    # imported as the package of tests.helpers before the loader runs
    "tests/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}

CLASS = "tests/test_executor.py::TestExecutor::"
EXECUTE_TPCDS = CLASS + "test_execute_tpcds"
EXECUTE_TPCH = CLASS + "test_execute_tpch"
OPTIMIZED_TPCH = CLASS + "test_optimized_tpch"

# Far above the pinned files (471 KB and 42 KB): setUpClass stays as fast as it was.
MAX_BYTES = 2 * 1024 * 1024

_INDEX_LINE = re.compile(rb"index [0-9a-f]{7,64}\.\.[0-9a-f]{7,64}(?: 100644)?\r?\n\Z")
# Basenames that can change how pytest runs, how git applies the patch, or what the
# interpreter imports, wherever they are.
_COMMIT_BLOCKERS = {"conftest.py", "sitecustomize.py", "usercustomize.py", "pyproject.toml", "setup.cfg",
                    "tox.ini", "pytest.ini", ".gitattributes"}


# --- the pinned loader, on bytes -------------------------------------------------------------

def _filter_comments(s):  # tests/helpers.py:11-12, verbatim
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _extract_meta(sql):  # tests/helpers.py:15-24, verbatim
    meta = {}
    sql_lines = sql.split("\n")
    i = 0
    while sql_lines[i].startswith("#"):
        key, val = sql_lines[i].split(":", maxsplit=1)
        meta[key.lstrip("#").strip()] = val.strip()
        i += 1
    sql = "\n".join(sql_lines[i:])
    return sql, meta


def read_text(data):
    """What open(path, encoding="utf-8").read() returns for these bytes: strict UTF-8 (a BOM is
    kept), then universal newlines (\\r\\n and lone \\r become \\n)."""
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def load_pairs(data):
    """list(load_sql_fixture_pairs(f)) of tests/helpers.py:44-55 for a file with these bytes.
    Raises what the pinned loader raises (UnicodeDecodeError, ValueError, IndexError)."""
    statements = _filter_comments(read_text(data)).split(";")
    size = len(statements)
    out = []
    for i in range(0, size, 2):
        if i + 1 < size:
            sql = statements[i].strip()
            sql, meta = _extract_meta(sql)
            expected = statements[i + 1].strip()
            out.append((meta, sql, expected))
    return out


# --- what each test consumes -----------------------------------------------------------------

def _meta_sql(pairs):
    """What _mp_execute consumes, in order: meta (with its key order) and sql."""
    return [(tuple(meta.items()), sql) for meta, sql, _ in pairs]


def _sql_expected(pairs):
    """What test_optimized_tpch consumes, in order."""
    return [(sql, expected) for _, sql, expected in pairs]


def _executes(pairs):
    """True when _mp_execute reaches subtestHelper/cached_execute for some pair (mp_execute
    returns None unless meta.get("execute") is truthy)."""
    return any(meta.get("execute") for meta, _, _ in pairs)


# --- preconditions ---------------------------------------------------------------------------

def _sha(src_root, path, read_src):
    data = read_src(src_root, path)
    return None if data is None else hashlib.sha256(data).hexdigest()


def _plain_new(fp, old):
    """The new bytes when this FilePatch is a plain text modification that git apply can only
    place where we do (every hunk matches exactly at its stated line, complete line counts, no
    header but `index`), else None."""
    if fp.binary or fp.mode_only or getattr(fp, "copy", False) or fp.odd or not fp.hunks:
        return None
    if fp.old_path != fp.new_path:
        return None
    if any(not _INDEX_LINE.match(line) for line in fp.header):
        return None
    if misplaced_hunk(fp) is not None:
        return None
    # git splits lines on \n only, we (and the patch parser) also on a lone \r: no \r at all.
    if b"\r" in old or any(b"\r" in c for h in fp.hunks for _, c in h.lines):
        return None
    src = old.splitlines(keepends=True)
    pos = 0
    out = []
    for h in fp.hunks:
        before = [c for t, c in h.lines if t != b"+"]
        after = [c for t, c in h.lines if t != b"-"]
        if len(before) != h.old_len or len(after) != h.new_len:
            return None
        start = h.old_start - 1 if h.old_len > 0 else h.old_start
        if start < pos or src[start:start + len(before)] != before:
            return None
        # git apply wants a hunk without trailing context at the end of the file (and would
        # look for that place elsewhere); one stated at line 1 matches at the beginning anyway.
        if h.lines and h.lines[-1][0] != b" " and start + len(before) != len(src):
            return None
        out.extend(src[pos:start])
        out.extend(after)
        pos = start + len(before)
    out.extend(src[pos:])
    new = b"".join(out)
    try:
        if apply(fp, old) != new:
            return None
    except Exception:  # noqa: BLE001 - any disagreement: not plain
        return None
    return new


def _commit_blocker(fp, file_patches):
    """Why another file of the commit can change the loader, the test module or how they run."""
    if file_patches is None:
        return "commit file list unknown"
    if not any(f is fp for f in file_patches):
        return "file patch not in the commit list"
    for f in file_patches:
        for p in (f.old_path, f.new_path):
            if p is None:
                continue
            if f is not fp and p in FIXTURES:
                return f"{p} is patched too"
            if f is not fp and p.startswith(("tests/fixtures/optimizer/tpc-h/", "tests/fixtures/optimizer/tpc-ds/")):
                return f"{p} is patched too"
            if p.startswith("tests/") and p.endswith(".py"):
                return f"{p} is patched"
            if posixpath.basename(p) in _COMMIT_BLOCKERS:
                return f"{p} is patched"
    return None


def _bit_index(maps):
    return {tid: i for i, tid in enumerate(maps.ids)}


def narrow(fp, path, old, src_root, maps, file_patches, read_src):
    """(bits, note) of the readers of `path` that can change outcome, or (None, reason)."""
    if path not in FIXTURES:
        return None, "not a TPC fixture"
    why = _commit_blocker(fp, file_patches)
    if why:
        return None, why
    for p, digest in PINNED.items():
        if _sha(src_root, p, read_src) != digest:
            return None, f"{p} is not the pinned file"
    new = _plain_new(fp, old)
    if new is None:
        return None, "not a plain modification applying at its stated lines"
    if len(new) > MAX_BYTES:
        return None, f"new file has {len(new)} bytes"
    other_path = TPCDS if path == TPCH else TPCH
    other = read_src(src_root, other_path)
    if other is None:
        return None, f"{other_path} missing"
    try:
        old_pairs = load_pairs(old)
        other_pairs = load_pairs(other)
    except Exception as error:  # noqa: BLE001
        return None, f"pinned fixture does not load ({error!r})"
    try:
        new_pairs = load_pairs(new)
    except Exception as error:  # noqa: BLE001 - setUpClass would fail: every reader
        return None, f"setUpClass would fail ({type(error).__name__})"

    index = _bit_index(maps)
    readers = maps.reads(path)
    executor = {tid for tid, i in index.items() if tid.startswith(CLASS) and readers >> i & 1}
    if not {EXECUTE_TPCDS, EXECUTE_TPCH, OPTIMIZED_TPCH} <= executor:
        return None, "maps do not list the TPC executor tests as readers"

    keep = set()
    other_sqls = {sql for _, sql, _ in other_pairs}
    if path == TPCDS:
        if _meta_sql(old_pairs) != _meta_sql(new_pairs):
            keep.add(EXECUTE_TPCDS)
            if other_sqls & {sql for _, sql, _ in new_pairs}:
                return None, "a tpc-ds query equals a tpc-h query (shared cache)"
    else:
        ex = _meta_sql(old_pairs) != _meta_sql(new_pairs)
        opt = _sql_expected(old_pairs) != _sql_expected(new_pairs)
        if _executes(new_pairs) and (ex or opt):
            ex = opt = True  # both read tpch_conn and the cache: one can change the other
        if ex:
            keep.add(EXECUTE_TPCH)
        if opt:
            keep.add(OPTIMIZED_TPCH)
        if keep and other_sqls & {sql for _, sql, _ in new_pairs}:
            return None, "a tpc-h query equals a tpc-ds query (shared cache)"

    drop = executor - keep
    bits = readers
    for tid in drop:
        bits &= ~(1 << index[tid])
    kept = ", ".join(sorted(t.rsplit("::", 1)[1] for t in keep)) or "none"
    note = (f"{path}: data readers; TPC fixture: dropped {len(drop)} TestExecutor test(s) that only need "
            f"setUpClass to load it (loads: {len(new_pairs)} pairs), kept executor tests: {kept}")
    return bits, note
