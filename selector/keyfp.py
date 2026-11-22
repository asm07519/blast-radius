"""Canonical, process-independent fingerprint of a hashable key: key_fp(obj) -> str | None.

Shared by the import-state diff (selector/state_probe.py) and the per-test read-set tracer:
both sides must produce the same string for keys that a dict lookup / set membership test
would treat as the same key, in any process, under any PYTHONHASHSEED. Keep this module
dependency-free (stdlib `enum` only), Python 3.9+ compatible and stable: changing an output
format invalidates every baseline and map built with the old one.

Formats (two keys get the same fingerprint only if they compare equal; None means "cannot be
keyed", and the caller then treats the whole container as read / changed):
    None                              "n"
    bool, int, integral float         "i:<int>"   True == 1 == 1.0 hash-equal, so they collide
    other float                       "f:<repr>"  (NaN -> None: it is only equal to itself)
    complex with imag == 0            as its real part; other complex "x:<repr>"
    str                               "s:<text>"
    bytes                             "y:<hex>"
    Enum member                       "e:<module>:<QualName>.<NAME>"
    class                             "c:<module>:<qualname>"
    tuple of fingerprintable items    "t:(" + ",".join(repr(item_fp)) + ")"
    frozenset of the same             "z:{" + ",".join(sorted(repr(item_fp))) + "}"
    anything else                     None

Subclasses: an instance of a subclass of str/int/float/bytes/tuple/frozenset that inherits
both __eq__ and __hash__ from that builtin compares and hashes like the builtin value, so it is
fingerprinted as that value (namedtuples become plain tuples). If either is overridden, None.

Enum members. A plain Enum member uses identity equality (object.__eq__) and Enum.__hash__
(hash of the name): it is equal only to itself, keyed by class and name. A mixin enum
(`class E(str, Enum)`, IntEnum, StrEnum, IntFlag...) resolves __eq__ and __hash__ to the mixin
type's slots, so the member compares equal to the plain mixin value (E.a == "x", and
{"x": 1}[E.a] works): it is fingerprinted as that underlying builtin value, extracted with the
builtin's own method so an overridden __str__/__int__ cannot interfere. Any other combination
(a custom __eq__ or __hash__ on the enum, or __eq__ and __hash__ from different classes) gives
None. Flag members follow the same rule (IntFlag is an int mixin; a plain Flag member uses
identity and is keyed by name; pseudo-members without a name give None).

Classes are keyed by module and qualname unless their metaclass overrides __eq__/__hash__
(then None). Two distinct classes with the same module:qualname would collide; the state
probe reports such duplicates separately.
"""

import enum

_IDENTITY_EQ = object.__eq__
_IDENTITY_HASH = object.__hash__
_ENUM_HASH = enum.Enum.__hash__

# Builtin value types and how to pull an exact builtin copy out of a subclass instance.
_BASES = (
    (bool, None),  # bool cannot be subclassed
    (int, int.__int__),
    (float, float.__float__),
    (str, str.__str__),
    (bytes, lambda b: bytes.__getitem__(b, slice(None))),
    (tuple, lambda t: tuple.__getitem__(t, slice(None))),
    (frozenset, frozenset.copy),
)


def _resolved(cls, name):
    """The attribute `name` as found on the MRO of `cls` (the defining class's dict entry)."""
    for klass in cls.__mro__:
        d = klass.__dict__
        if name in d:
            return d[name]
    return None


def _same_slots(cls, base):
    return (_resolved(cls, "__eq__") is base.__dict__.get("__eq__", _IDENTITY_EQ)
            and _resolved(cls, "__hash__") is base.__dict__.get("__hash__", _IDENTITY_HASH))


def _builtin(obj):
    """(fp) for exact builtins; None when obj is not a builtin value type."""
    t = type(obj)
    if obj is None:
        return "n"
    if t is bool or t is int:
        return "i:%d" % int(obj)
    if t is float:
        return _float(obj)
    if t is str:
        return "s:" + obj
    if t is bytes:
        return "y:" + obj.hex()
    if t is complex:
        return _float(obj.real) if obj.imag == 0 else "x:" + repr(obj)
    if t is tuple:
        parts = [key_fp(x) for x in obj]
        if any(p is None for p in parts):
            return None
        return "t:(" + ",".join(repr(p) for p in parts) + ")"
    if t is frozenset:
        parts = [key_fp(x) for x in obj]
        if any(p is None for p in parts):
            return None
        return "z:{" + ",".join(sorted(repr(p) for p in parts)) + "}"
    return None


def _float(x):
    if x != x:
        return None  # NaN: equal only to the identical object
    if x in (float("inf"), float("-inf")):
        return "f:" + repr(x)
    if x == int(x):
        return "i:%d" % int(x)
    return "f:" + repr(x)


def key_fp(obj):
    try:
        return _key_fp(obj)
    except Exception:  # noqa: BLE001 - anything odd cannot be keyed
        return None


def _key_fp(obj):
    t = type(obj)
    if t in (type(None), bool, int, float, str, bytes, complex, tuple, frozenset):
        return _builtin(obj)
    if isinstance(obj, type):
        meta = type(obj)
        if _resolved(meta, "__eq__") is not _IDENTITY_EQ or _resolved(meta, "__hash__") is not type.__dict__.get(
                "__hash__", _IDENTITY_HASH):
            return None
        return "c:%s:%s" % (obj.__module__, obj.__qualname__)
    if isinstance(obj, enum.Enum):
        eq, hs = _resolved(t, "__eq__"), _resolved(t, "__hash__")
        if eq is _IDENTITY_EQ and hs is _ENUM_HASH:
            name = obj._name_
            if not isinstance(name, str):
                return None  # e.g. a composite Flag pseudo-member
            return "e:%s:%s.%s" % (t.__module__, t.__qualname__, name)
        for base, extract in _BASES:
            if extract is not None and isinstance(obj, base) and _same_slots(t, base):
                return _builtin(extract(obj))
        return None
    for base, extract in _BASES:
        if extract is not None and isinstance(obj, base):
            if _same_slots(t, base):
                return _builtin(extract(obj))
            return None
    return None
