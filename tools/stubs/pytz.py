"""Stand-in for pytz built on zoneinfo, used only where pytz is missing.

Uses during a test body are recorded so the map builder can mark that test's
coverage as untrusted.
"""

import datetime as _dt
import os
import sys as _sys
import zoneinfo as _zi


def _imported_by_pandas():
    f = _sys._getframe(1)
    while f is not None:
        if "/pandas/" in f.f_code.co_filename:
            return True
        f = f.f_back
    return False


if _imported_by_pandas():
    # pandas treats pytz as optional; keep it seeing "not installed".
    raise ImportError("pytz stand-in is not offered to pandas")

__version__ = "2025.2"


def _record():
    try:
        import br_tracer

        phase = os.environ.get("BR_PHASE", "collect")
        if phase not in br_tracer._extra.get("duckdb", []):
            br_tracer.note("duckdb", phase)
    except Exception:
        pass


class _TZ(_zi.ZoneInfo):
    @property
    def zone(self):
        return self.key

    def localize(self, value, is_dst=False):
        _record()
        return value.replace(tzinfo=self)


def timezone(name):
    _record()
    return _TZ(name)


utc = UTC = _dt.timezone.utc
