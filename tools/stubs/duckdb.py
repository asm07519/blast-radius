"""Stand-in for duckdb, used only where the real package is missing.

Every use is recorded with the pytest phase it happened in. A test whose body
(phase "call") touches this stand-in has unreliable coverage, so the map builder
marks it as untrusted and the selector treats it conservatively.
"""

import os

__version__ = "0.0.0-stub"


def _record():
    try:
        import br_tracer

        phase = os.environ.get("BR_PHASE", "collect")
        if phase not in br_tracer._extra.get("duckdb", []):
            br_tracer.note("duckdb", phase)
    except Exception:
        pass


class _Any:
    def __init__(self, *args, **kwargs):
        _record()

    def __getattr__(self, name):
        _record()
        return _Any()

    def __call__(self, *args, **kwargs):
        _record()
        return _Any()

    def __enter__(self):
        _record()
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        _record()
        return iter(())

    def __len__(self):
        _record()
        return 0

    def __bool__(self):
        _record()
        return True


def connect(*args, **kwargs):
    _record()
    return _Any()


def __getattr__(name):
    _record()
    return _Any
