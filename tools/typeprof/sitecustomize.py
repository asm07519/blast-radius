"""Type profile of identity/equality comparisons (map building only, never at run time).

With BR_TYPEPROF_DIR and BR_REPO set, every sqlglot module is compiled from an AST
in which each single-operator comparison `a is b`, `a is not b`, `a == b`,
`a != b` becomes `__br_cmp__(site, op, a, b)`. That evaluates `a` then `b` and
returns exactly `a <op> b`, and records the pair of operand types seen at that
site, keyed by the comparison's original position "file:line:col".

Each process writes <pid>.json at exit: {site: [[left_key, left_cat, right_key, right_cat], ...]}
where *_cat says how the type's == behaves (see classify()).
"""

import os
import sys


def _chain():
    here = os.path.dirname(os.path.abspath(__file__))
    for entry in sys.path:
        if not entry or os.path.abspath(entry) == here:
            continue
        candidate = os.path.join(entry, "sitecustomize.py")
        if os.path.isfile(candidate):
            code = compile(open(candidate, "rb").read(), candidate, "exec")
            exec(code, {"__name__": "_chained_sitecustomize", "__file__": candidate})
            return


try:
    _chain()
except Exception as error:
    print(f"[typeprof] chained sitecustomize failed: {error!r}", file=sys.stderr)

if os.environ.get("BR_TYPEPROF_DIR"):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import br_typeprof

    br_typeprof.install()
