"""Cell-read tracer: which import-level tables (and which keys of them) a process reads.

Used only by tools/build_cells.py (BR_CELLS=1, on top of br_tracer); never at run time.

A *cell* is a binding that holds a value after import:
    ("c", "<module>:<Qual.Name>", attr)   an entry of a sqlglot class __dict__
    ("g", "<module>", name)               a global of a sqlglot module

When the outermost import of a sqlglot module finishes, every exact dict / set /
frozenset held by a cell (module globals of every loaded sqlglot.* module, the
__dict__ of every sqlglot class reachable from them, nested classes and
subclasses included) is replaced, in every binding that holds it (by identity),
by one logging subclass instance with the same data and the same iteration
order. References to the original that are not cells (dict values, list items,
closure cells, instance __dict__ entries) are redirected to the proxy too;
references that cannot be redirected (tuples, frozensets, function defaults,
frames) make every cell of that object *leaky* (the consumer must not trust key
evidence for it).

Reads are logged per proxy into one of two channels:
    "test"   anything that runs while no sqlglot module is being executed
             (collection, test-module code, fixtures, setUpClass, test body,
             teardown: each test runs alone in its own process, so all of it
             belongs to the test)
    "import" reads made while some sqlglot module's code is executing (import-time
             derivation of other cells; a lazily imported dialect counts here too)

Keyed operations log the key; every other read logs a *whole* read. Mutations
are logged (keyed mutations that observe presence, such as pop/remove/del, also
log the key). Computed-name attribute access through the getattr / hasattr /
setattr / delattr / vars / dir / inspect.getmembers builtins is logged with the
object's class (or module).

Alarms (the test is then not trusted): a cell rebound away from its proxy, a
setattr() that rebinds a tracked cell, the proxy machinery itself failing.
"""

import builtins
import gc
import os
import sys

_KEYS = {"test": {}, "import": {}}     # channel -> proxy index -> set of raw keys
_WHOLE = {"test": set(), "import": set()}
_MUT = {"test": set(), "import": set()}
_UNHASHABLE = {"test": set(), "import": set()}  # keyed op with an unhashable key: whole
_DYN = {"test": set(), "import": set()}  # (family, name, op)
_CH = "test"
_CUR_KEYS = _KEYS["test"]
_CUR_WHOLE = _WHOLE["test"]
_CUR_MUT = _MUT["test"]
_CUR_UNH = _UNHASHABLE["test"]

_PROXIES = []        # index -> proxy
_ORIGINALS = []      # index -> original (kept alive so ids stay unique)
_BY_ORIG = {}        # id(original) -> index
_CELLS = {}          # (cell, id(holder)) -> (holder, name, index); holder: module dict or class
_KIND = []           # index -> "dict" | "set" | "frozenset"
_LEAKS = {}          # index -> sorted reasons
_UNPROXIED = {}      # cell -> reason (exact dict/set/frozenset left as is)
_ALARMS = []
_COLLIDE = set()     # class keys bound to two distinct classes
_CLASS_OF_KEY = {}
_depth = 0
_installed = False


def _set_channel(ch):
    global _CH, _CUR_KEYS, _CUR_WHOLE, _CUR_MUT, _CUR_UNH
    _CH = ch
    _CUR_KEYS = _KEYS[ch]
    _CUR_WHOLE = _WHOLE[ch]
    _CUR_MUT = _MUT[ch]
    _CUR_UNH = _UNHASHABLE[ch]


def _key(i, k):
    try:
        s = _CUR_KEYS.get(i)
        if s is None:
            s = _CUR_KEYS[i] = set()
        s.add(k)
    except TypeError:
        _CUR_UNH.add(i)


def _whole(i):
    _CUR_WHOLE.add(i)


def _mut(i):
    _CUR_MUT.add(i)


def alarm(reason):
    if len(_ALARMS) < 50:
        _ALARMS.append(reason)


# ---------------------------------------------------------------- proxies

_dict_items = dict.items


def _plain_dict(p):
    return dict(_dict_items(p))  # an items view is not a mapping: no override is called


class DictProxy(dict):
    __slots__ = ("_br_i",)

    # keyed reads
    def __getitem__(self, k):
        _key(self._br_i, k)
        return dict.__getitem__(self, k)

    def __contains__(self, k):
        _key(self._br_i, k)
        return dict.__contains__(self, k)

    def get(self, k, *default):
        _key(self._br_i, k)
        return dict.get(self, k, *default)

    def setdefault(self, k, *default):
        _key(self._br_i, k)
        if not dict.__contains__(self, k):
            _mut(self._br_i)
        return dict.setdefault(self, k, *default)

    def pop(self, k, *default):
        _key(self._br_i, k)
        _mut(self._br_i)
        return dict.pop(self, k, *default)

    def __delitem__(self, k):
        _key(self._br_i, k)
        _mut(self._br_i)
        return dict.__delitem__(self, k)

    # mutations that observe nothing
    def __setitem__(self, k, v):
        _mut(self._br_i)
        return dict.__setitem__(self, k, v)

    def update(self, *a, **kw):
        _mut(self._br_i)
        return dict.update(self, *a, **kw)

    def clear(self):
        _mut(self._br_i)
        return dict.clear(self)

    def popitem(self):
        _whole(self._br_i)
        _mut(self._br_i)
        return dict.popitem(self)

    def __ior__(self, other):
        _mut(self._br_i)
        return dict.__ior__(self, other)

    # whole reads
    def __iter__(self):
        _whole(self._br_i)
        return dict.__iter__(self)

    def __reversed__(self):
        _whole(self._br_i)
        return dict.__reversed__(self)

    def __len__(self):
        _whole(self._br_i)
        return dict.__len__(self)

    def __bool__(self):
        _whole(self._br_i)
        return dict.__len__(self) != 0

    def keys(self):
        _whole(self._br_i)
        return dict.keys(self)

    def values(self):
        _whole(self._br_i)
        return dict.values(self)

    def items(self):
        _whole(self._br_i)
        return dict.items(self)

    def copy(self):
        _whole(self._br_i)
        return _plain_dict(self)

    def __copy__(self):
        _whole(self._br_i)
        return _plain_dict(self)

    def __deepcopy__(self, memo):
        _whole(self._br_i)
        from copy import deepcopy

        y = {}
        memo[id(self)] = y
        for k, v in _dict_items(self):
            y[deepcopy(k, memo)] = deepcopy(v, memo)
        return y

    def __reduce_ex__(self, protocol):
        _whole(self._br_i)
        return (dict, (_plain_dict(self),))

    def __reduce__(self):
        _whole(self._br_i)
        return (dict, (_plain_dict(self),))

    def __eq__(self, other):
        _whole(self._br_i)
        if isinstance(other, (DictProxy, SetProxy, FrozenSetProxy)):
            _whole(other._br_i)
        return dict.__eq__(self, other)

    def __ne__(self, other):
        _whole(self._br_i)
        if isinstance(other, (DictProxy, SetProxy, FrozenSetProxy)):
            _whole(other._br_i)
        return dict.__ne__(self, other)

    __hash__ = None

    def __or__(self, other):
        _whole(self._br_i)
        return dict.__or__(self, other)

    def __ror__(self, other):
        _whole(self._br_i)
        return dict.__ror__(self, other)

    def __repr__(self):
        _whole(self._br_i)
        return dict.__repr__(self)

    __str__ = __repr__

    def __format__(self, spec):
        _whole(self._br_i)
        return format(_plain_dict(self), spec)

    def __sizeof__(self):
        _whole(self._br_i)
        return dict.__sizeof__(self)

    @classmethod
    def fromkeys(cls, *a):
        return dict.fromkeys(*a)


def _set_methods(base, name):
    """Logging overrides shared by the set and frozenset proxies."""
    ns = {"__slots__": ("_br_i",)}

    def keyed(attr):
        f = getattr(base, attr)

        def m(self, k):
            _key(self._br_i, k)
            return f(self, k)

        m.__name__ = attr
        return m

    def whole(attr):
        f = getattr(base, attr)

        def m(self, *a):
            _whole(self._br_i)
            for x in a:
                if isinstance(x, (DictProxy, SetProxy, FrozenSetProxy)):
                    _whole(x._br_i)
            return f(self, *a)

        m.__name__ = attr
        return m

    ns["__contains__"] = keyed("__contains__")
    for attr in ("__iter__", "__len__", "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__",
                 "__or__", "__ror__", "__and__", "__rand__", "__sub__", "__rsub__", "__xor__", "__rxor__",
                 "union", "intersection", "difference", "symmetric_difference", "issubset", "issuperset",
                 "isdisjoint", "copy", "__sizeof__"):
        ns[attr] = whole(attr)

    def __bool__(self):
        _whole(self._br_i)
        return base.__len__(self) != 0

    def __repr__(self):
        _whole(self._br_i)
        return repr(base(base.__iter__(self)))

    def __format__(self, spec):
        _whole(self._br_i)
        return format(base(base.__iter__(self)), spec)

    def __reduce_ex__(self, protocol):
        _whole(self._br_i)
        return (base, (list(base.__iter__(self)),))

    def __reduce__(self):
        _whole(self._br_i)
        return (base, (list(base.__iter__(self)),))

    def __copy__(self):
        _whole(self._br_i)
        return base(base.__iter__(self)) if base is set else base.copy(self)

    def __deepcopy__(self, memo):
        _whole(self._br_i)
        from copy import deepcopy

        # copy.deepcopy reduces a plain set/frozenset: base(deepcopy([items]))
        y = base(deepcopy(list(base.__iter__(self)), memo))
        memo[id(self)] = y
        return y

    ns.update(__bool__=__bool__, __repr__=__repr__, __str__=__repr__, __format__=__format__,
              __reduce_ex__=__reduce_ex__, __reduce__=__reduce__, __copy__=__copy__,
              __deepcopy__=__deepcopy__)
    if base is frozenset:
        def __hash__(self):
            _whole(self._br_i)
            return frozenset.__hash__(self)

        ns["__hash__"] = __hash__
    else:
        ns["__hash__"] = None

        def mutating(attr, keyed_too=False, whole_too=False):
            f = getattr(base, attr)

            def m(self, *a):
                if keyed_too and a:
                    _key(self._br_i, a[0])
                if whole_too:
                    _whole(self._br_i)
                _mut(self._br_i)
                return f(self, *a)

            m.__name__ = attr
            return m

        for attr in ("add", "discard", "clear", "update", "intersection_update", "difference_update",
                     "symmetric_difference_update", "__ior__", "__iand__", "__isub__", "__ixor__"):
            ns[attr] = mutating(attr)
        ns["remove"] = mutating("remove", keyed_too=True)
        ns["pop"] = mutating("pop", whole_too=True)
        # the in-place operators read self too (iand/isub keep or drop by content): log whole
        for attr in ("intersection_update", "difference_update", "symmetric_difference_update",
                     "__iand__", "__isub__", "__ixor__"):
            ns[attr] = mutating(attr, whole_too=True)
    return type(name, (base,), ns)


SetProxy = _set_methods(set, "SetProxy")
FrozenSetProxy = _set_methods(frozenset, "FrozenSetProxy")
PROXY_TYPES = (DictProxy, SetProxy, FrozenSetProxy)


def _same_order(orig, p):
    """Iteration order of the proxy matches the original's, element by identity."""
    xs = list(iter(orig))
    ys = list(type(p).__mro__[1].__iter__(p))
    return len(xs) == len(ys) and all(x is y for x, y in zip(xs, ys))


def make_proxy(v):
    """A logging copy of v with the same data and iteration order, or None."""
    t = type(v)
    if t is dict:
        p = DictProxy()
        dict.update(p, v)
        return p
    if t is set:
        p = SetProxy(v)
        if not _same_order(v, p):
            p = SetProxy()
            for x in v:
                set.add(p, x)
            if not _same_order(v, p):
                return None
        return p
    if t is frozenset:
        p = FrozenSetProxy(v)
        if not _same_order(v, p):
            p = FrozenSetProxy(list(v))
            if not _same_order(v, p):
                return None
        return p
    return None


def _register(v):
    i = _BY_ORIG.get(id(v))
    if i is not None:
        return i
    p = make_proxy(v)
    if p is None:
        return None
    i = len(_PROXIES)
    p._br_i = i
    _PROXIES.append(p)
    _ORIGINALS.append(v)
    _BY_ORIG[id(v)] = i
    _KIND.append(type(v).__name__)
    return i


# ---------------------------------------------------------------- installing

def _is_sqlglot(name):
    return isinstance(name, str) and (name == "sqlglot" or name.startswith("sqlglot."))


def _skip_attr(name):
    return not isinstance(name, str) or (name.startswith("__") and name.endswith("__")) or (
        len(name) > 2 and name.startswith("_") and name.endswith("_") and not name.startswith("__"))


def _class_key(cls):
    return f"{cls.__module__}:{cls.__qualname__}"


_EXACT = (dict, set, frozenset)


def _bind(cell, holder, name, v, is_module):
    slot = (cell, id(holder))  # two classes can share "module:qualname" (see _COLLIDE)
    if slot in _CELLS:
        h, n, i = _CELLS[slot]
        if v is _PROXIES[i]:
            return None
        if isinstance(v, PROXY_TYPES) and v is not _PROXIES[i]:
            alarm(f"rebound to another proxy: {cell}")
        elif type(v) in _EXACT and _BY_ORIG.get(id(v)) != i:
            alarm(f"rebound: {cell}")
        elif not isinstance(v, PROXY_TYPES) and type(v) not in _EXACT:
            alarm(f"rebound to a {type(v).__name__}: {cell}")
    t = type(v)
    if isinstance(v, PROXY_TYPES):
        _CELLS[slot] = (holder, name, v._br_i)
        return None
    if t not in _EXACT:
        _CELLS.pop(slot, None)
        return None
    i = _register(v)
    if i is None:
        _UNPROXIED[cell] = "iteration order"
        return None
    p = _PROXIES[i]
    try:
        if is_module:
            holder[name] = p
        else:
            type.__setattr__(holder, name, p)
    except Exception as error:
        _UNPROXIED[cell] = f"cannot rebind: {error!r}"[:200]
        return None
    _CELLS[slot] = (holder, name, i)
    return v


def _walk_class(cls, seen, fresh):
    if id(cls) in seen:
        return
    seen.add(id(cls))
    if not _is_sqlglot(getattr(cls, "__module__", None)):
        return
    key = _class_key(cls)
    other = _CLASS_OF_KEY.setdefault(key, cls)
    if other is not cls:
        _COLLIDE.add(key)
    try:
        items = list(cls.__dict__.items())
    except Exception:
        return
    for name, v in items:
        if _skip_attr(name):
            continue
        orig = _bind(("c", key, name), cls, name, v, False)
        if orig is not None:
            fresh.append(orig)
        if isinstance(v, type):
            _walk_class(v, seen, fresh)
    try:
        subs = type.__subclasses__(cls)
    except Exception:
        subs = ()
    for s in subs:
        _walk_class(s, seen, fresh)


def proxify():
    """One round: proxy every exact table held by a cell of a loaded sqlglot module."""
    fresh = []
    seen = set()
    for mname, m in list(sys.modules.items()):
        if not _is_sqlglot(mname) or m is None:
            continue
        md = getattr(m, "__dict__", None)
        if not isinstance(md, dict):
            continue
        mname = getattr(m, "__name__", mname)
        for name, v in list(md.items()):
            if _skip_attr(name):
                continue
            orig = _bind(("g", mname, name), md, name, v, True)
            if orig is not None:
                fresh.append(orig)
            if isinstance(v, type):
                _walk_class(v, seen, fresh)
    if fresh:
        _redirect(fresh)


def _redirect(fresh):
    """Point remaining references to the originals at their proxies, or mark them leaky."""
    me = sys._getframe()
    skip = {id(_ORIGINALS), id(fresh), id(me)}
    orig_ids = {id(o) for o in fresh}
    all_orig = set(_BY_ORIG)
    for ref in gc.get_referrers(*fresh):
        if id(ref) in skip:
            continue
        if id(ref) in all_orig:
            continue  # an original refers to another original: dead data
        t = type(ref)
        if t is tuple and len(ref) == len(fresh) and all(a is b for a, b in zip(ref, fresh)):
            continue  # the argument tuple of this get_referrers call
        try:
            if t is dict or isinstance(ref, DictProxy):
                for k, v in list(_dict_items(ref)):
                    if id(v) in orig_ids:
                        dict.__setitem__(ref, k, _PROXIES[_BY_ORIG[id(v)]])
                    if id(k) in orig_ids:
                        _leak(_BY_ORIG[id(k)], "dict key")
            elif t is list:
                for j, v in enumerate(ref):
                    if id(v) in orig_ids:
                        ref[j] = _PROXIES[_BY_ORIG[id(v)]]
            elif t.__name__ == "cell":
                v = ref.cell_contents
                if id(v) in orig_ids:
                    ref.cell_contents = _PROXIES[_BY_ORIG[id(v)]]
            else:
                what = t.__name__
                if t.__name__ == "frame":
                    what = f"frame {ref.f_code.co_filename}:{ref.f_lineno}"
                elif t is tuple:
                    what = "tuple"
                for o in fresh:
                    if any(x is o for x in gc.get_referents(ref)):
                        _leak(_BY_ORIG[id(o)], what)
        except Exception as error:
            for o in fresh:
                _leak(_BY_ORIG[id(o)], f"redirect failed: {error!r}"[:120])


def _leak(i, reason):
    _LEAKS.setdefault(i, set()).add(reason[:160])


def check_bindings(where):
    """Alarm for every cell that no longer holds its proxy."""
    for slot, (holder, name, i) in list(_CELLS.items()):
        cell = slot[0]
        try:
            cur = holder.get(name, _MISSING) if type(holder) is dict else holder.__dict__.get(name, _MISSING)
        except Exception:
            cur = _MISSING
        if cur is not _PROXIES[i]:
            alarm(f"{where}: {cell} no longer holds its proxy ({type(cur).__name__})")
            _CELLS.pop(slot, None)


_MISSING = object()


# ---------------------------------------------------------------- dynamic names

def _family(obj):
    try:
        if isinstance(obj, type(sys)):
            n = getattr(obj, "__name__", "")
            return ("m:" + n) if _is_sqlglot(n) else None
        cls = obj if isinstance(obj, type) else type(obj)
        for k in cls.__mro__:
            if _is_sqlglot(getattr(k, "__module__", None)):
                return ("c:" if cls is obj else "i:") + _class_key(cls)
    except Exception:
        return None
    return None


def _dyn(obj, name, op):
    fam = _family(obj)
    if fam is not None:
        _DYN[_CH].add((fam, name if isinstance(name, str) else "?", op))


_real = {}


def _install_builtins():
    import inspect

    _real.update(getattr=builtins.getattr, hasattr=builtins.hasattr, setattr=builtins.setattr,
                 delattr=builtins.delattr, vars=builtins.vars, dir=builtins.dir, getmembers=inspect.getmembers)
    rg, rh, rs, rd, rv, rdir, rgm = (_real[k] for k in ("getattr", "hasattr", "setattr", "delattr", "vars",
                                                         "dir", "getmembers"))
    _NO = object()

    def getattr(obj, name, default=_NO):
        _dyn(obj, name, "get")
        if default is _NO:
            return rg(obj, name)
        return rg(obj, name, default)

    def hasattr(obj, name):
        _dyn(obj, name, "has")
        return rh(obj, name)

    def setattr(obj, name, value):
        _dyn(obj, name, "set")
        if isinstance(obj, type) and isinstance(name, str) and _CELLS:
            if _is_sqlglot(getattr_raw(obj, "__module__")):
                cell = ("c", _class_key(obj), name)
                if (cell, id(obj)) in _CELLS:
                    alarm(f"setattr rebinds tracked {cell}")
        elif isinstance(obj, type(sys)) and isinstance(name, str) and _is_sqlglot(obj.__name__):
            md = getattr(obj, "__dict__", None)
            if md is not None and (("g", obj.__name__, name), id(md)) in _CELLS:
                alarm(f"setattr rebinds tracked global {obj.__name__}.{name}")
        return rs(obj, name, value)

    def delattr(obj, name):
        _dyn(obj, name, "del")
        if isinstance(obj, type) and isinstance(name, str) and (("c", _class_key(obj), name), id(obj)) in _CELLS:
            alarm(f"delattr removes tracked {_class_key(obj)}.{name}")
        elif isinstance(obj, type(sys)) and isinstance(name, str) and _is_sqlglot(obj.__name__):
            md = getattr(obj, "__dict__", None)
            if md is not None and (("g", obj.__name__, name), id(md)) in _CELLS:
                alarm(f"delattr removes tracked global {obj.__name__}.{name}")
        return rd(obj, name)

    def vars(*a):
        if a:
            _dyn(a[0], "*", "vars")
        return rv(*a) if a else sys._getframe(1).f_locals

    def dir(*a):
        if a:
            _dyn(a[0], "*", "dir")
            return rdir(a[0])
        return sorted(sys._getframe(1).f_locals)

    def getmembers(obj, predicate=None):
        _dyn(obj, "*", "getmembers")
        return rgm(obj, predicate)

    def getattr_raw(obj, name):
        try:
            return type.__getattribute__(obj, name)
        except Exception:
            return None

    builtins.getattr, builtins.hasattr, builtins.setattr, builtins.delattr = getattr, hasattr, setattr, delattr
    builtins.vars, builtins.dir = vars, dir
    inspect.getmembers = getmembers


# ---------------------------------------------------------------- import hook

def _patch_imports():
    import importlib._bootstrap_external as be

    original = be._LoaderBasics.exec_module

    def exec_module(self, module):
        global _depth
        name = getattr(module, "__name__", "")
        if not _is_sqlglot(name):
            return original(self, module)
        _depth += 1
        if _depth == 1:
            _set_channel("import")
        try:
            return original(self, module)
        finally:
            _depth -= 1
            if _depth == 0:
                try:
                    proxify()
                except Exception as error:
                    alarm(f"proxify failed: {error!r}"[:300])
                _set_channel("test")

    be._LoaderBasics.exec_module = exec_module


def install():
    global _installed
    if _installed:
        return
    _installed = True
    _patch_imports()
    _install_builtins()
    if any(_is_sqlglot(n) for n in sys.modules):
        alarm("sqlglot imported before br_cells was installed")


# ---------------------------------------------------------------- output

def _load_key_fp():
    """selector/keyfp.py by path (putting the app root on sys.path could shadow tests/)."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "selector", "keyfp.py")
    try:
        spec = importlib.util.spec_from_file_location("_br_keyfp", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.key_fp
    except Exception as error:
        alarm(f"selector/keyfp.py not loadable: {error!r}"[:200])
        return None


def dump():
    """JSON-able summary for this process (keys as canonical key_fp strings)."""
    try:
        check_bindings("exit")
    except Exception as error:
        alarm(f"check failed: {error!r}")
    key_fp = _load_key_fp()
    cells_of = {}
    for (cell, _), (_, _, i) in _CELLS.items():
        cells_of.setdefault(i, set()).add(cell)
    out = {"alarms": list(_ALARMS), "proxies": [], "unproxied": sorted([list(c), r] for c, r in _UNPROXIED.items()),
           "collisions": sorted(_COLLIDE)}
    for i, p in enumerate(_PROXIES):
        cells = sorted(list(c) for c in cells_of.get(i, ()))
        rec = {"kind": _KIND[i], "cells": cells, "leaks": sorted(_LEAKS.get(i, ()))}
        for ch in ("test", "import"):
            keys = _KEYS[ch].get(i, ())
            fps = set()
            whole = i in _WHOLE[ch] or i in _UNHASHABLE[ch]
            for k in list(keys):
                fp = key_fp(k) if key_fp else None
                if fp is None:
                    whole = True
                else:
                    fps.add(fp)
            rec[ch] = {"keys": sorted(fps), "whole": whole, "mut": i in _MUT[ch]}
        if cells or rec["test"]["keys"] or rec["test"]["whole"]:
            out["proxies"].append(rec)
    out["dyn"] = {ch: sorted(list(x) for x in _DYN[ch]) for ch in ("test", "import")}
    return out
