"""Starts the map tracer in every Python process that has BR_TRACE_DIR set.

Any other sitecustomize further down sys.path is run first, so an image's own
startup hook keeps working.
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
except Exception as error:  # never break interpreter start-up
    print(f"[br] chained sitecustomize failed: {error!r}", file=sys.stderr)

if os.environ.get("BR_TRACE_DIR"):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import br_tracer

    br_tracer.start()
