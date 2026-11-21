"""Fingerprint the cheap phase of sqlglot's executor tests (see tools/exec_phases.py).

Mirrors what test_execute_tpch/tpcds do before running the Python executor: per query
parse_one, a pickle round trip, find_tables, ensure_schema, optimize, a pretty render
(evaluated for a debug log), and transpile(sql, write="duckdb") for the duckdb side.
The optimized expressions are fingerprinted with our own walker over `.args`,
`_type` and `_meta`, never with sqlglot's own rendering, which a patch could change.
"""

import enum
import hashlib
import pickle


def _digest(text):
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=12).hexdigest()


def canon(root):
    from sqlglot import exp

    out = []
    memo = {}
    stack = [root]
    # iterative pre-order walk; repeated objects are referenced by their first position
    while stack:
        x = stack.pop()
        if isinstance(x, str) and x.startswith("\x00"):
            out.append(x)
            continue
        if x is None or isinstance(x, (bool, int, float, str, bytes)):
            out.append(repr(x))
            continue
        if isinstance(x, enum.Enum):
            out.append(f"{type(x).__name__}.{x.name}")
            continue
        key = id(x)
        if key in memo:
            out.append(f"@{memo[key]}")
            continue
        memo[key] = len(memo)
        if isinstance(x, exp.Expr):
            items = []
            for k in sorted(x.args):
                items += ["\x00" + k + "=", x.args[k]]
            items += ["\x00T=", getattr(x, "_type", None), "\x00M=", getattr(x, "_meta", None),
                      "\x00C=", getattr(x, "comments", None), "\x00)"]
            out.append("(" + type(x).__name__)
            stack.extend(reversed(items))
        elif isinstance(x, (list, tuple)):
            out.append("[" if isinstance(x, list) else "(")
            stack.append("\x00]")
            stack.extend(reversed(list(x)))
        elif isinstance(x, dict):
            out.append("{")
            stack.append("\x00}")
            items = sorted(x.items(), key=lambda kv: repr(kv[0]))
            for k, v in reversed(items):
                stack.append(v)
                stack.append("\x00" + repr(k) + ":")
        elif isinstance(x, (set, frozenset)):
            out.append("{" + ",".join(sorted(repr(v) for v in x)) + "}")
        else:
            out.append(type(x).__name__ + "?" + repr(sorted(getattr(x, "__dict__", {}).keys())))
    return "".join(out)


def _err(error):
    return f"raise {type(error).__name__}"


def fingerprint_suite(repo, suite, keep=False):
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema
    from tests import helpers

    schema_src = getattr(helpers, suite["schema"])
    queries = []
    parsed_keep, optimized_keep = [], []
    for meta, sql, _ in helpers.load_sql_fixture_pairs(suite["fixture"]):
        entry = {}
        try:
            e = pickle.loads(pickle.dumps(parse_one(sql)))
        except Exception as error:
            queries.append({"parse": _err(error)})
            continue
        if not meta.get("execute"):
            queries.append({"skip": True})
            continue
        try:
            entry["tables"] = sorted(t.name for t in find_tables(e))
            schema = ensure_schema(schema_src, dialect=None)
            entry["sta"] = repr(schema.supported_table_args)
            o = optimize(e, schema, leave_tables_isolated=True, dialect=None)
            entry["optimized"] = _digest(canon(o))
            try:
                o.sql(pretty=True)
                entry["rendered"] = "ok"
            except Exception as error:
                entry["rendered"] = _err(error)
            if keep:
                parsed_keep.append(e)
                optimized_keep.append(o)
        except Exception as error:
            entry["error"] = _err(error)
        try:
            entry["duck"] = _digest(transpile(sql, write="duckdb")[0])
        except Exception as error:
            entry["duck"] = _err(error)
        queries.append(entry)
    return {"queries": queries}, parsed_keep, optimized_keep
