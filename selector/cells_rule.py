"""BR_CELLS=1 selection rule: map state_probe differing cells to affected tests.

Called from selector/impact.py when BR_CELLS=1 and every sqlglot/ import site of a commit is
statically eligible (selector/cells_static.py). Runs state_probe once per commit; falls back
to all importers on any probe failure, environment side effect, opaque value, warm cache,
module-set change, collision cell, or disallowed dunder change.

When maps/cells.bin is loaded (maps.has_cells), tracked dict/set cells with attributable
key-level diffs narrow to key_readers(cell, k) plus whole/dynamic readers. Without
maps/cells.bin (or for untracked cells / whole changes), falls back to class-users /
importers intersected with name_readers and dynamic_readers.
"""

import ast
import os

_ALLOWED_DUNDERS = frozenset({"__annotations__", "__doc__"})
_DISALLOWED_SYNTHETIC = frozenset({"__bases__", "__class__"})


def _is_dunder(name):
    return isinstance(name, str) and len(name) > 4 and name.startswith("__") and name.endswith("__")


def _module_to_path(module, maps):
    base = module.replace(".", "/")
    for cand in (base + ".py", base + "/__init__.py"):
        if maps.file_known(cand):
            return cand
    return None


def _callee_problem(callees, patched_paths, src_root):
    """Verify every non-safe sqlglot callee referenced by a changed import site belongs to an
    unchanged pinned sqlglot module, or return a reason string."""
    if not callees:
        return None
    for resolved, safe in callees:
        if safe:
            continue
        parts = resolved.split(".")
        if not parts or parts[0] != "sqlglot":
            return f"callee {resolved} outside sqlglot"
        # Find the longest prefix that corresponds to a .py or /__init__.py file in src_root.
        found_path = None
        for i in range(len(parts), 0, -1):
            base = "/".join(parts[:i])
            for cand in (base + ".py", base + "/__init__.py"):
                if os.path.isfile(os.path.join(src_root, cand)):
                    found_path = cand
                    break
            if found_path is not None:
                break
        if found_path is None:
            return f"callee {resolved} module not found"
        if found_path in patched_paths:
            return f"callee {resolved} in patched file {found_path}"
    return None


def _class_family_users(key, maps):
    """Tests that can observe a class cell of `key`, or None if `key` is invisible to the maps."""
    info = maps.class_info.get(key)
    if info is None or not info[0]:
        return None
    users = maps.class_users(key)
    mod, qual = key.split(":", 1)
    top = qual.split(".", 1)[0]
    refs = {top}
    # If `key` is nested inside an outer class (e.g. a dialect's Tokenizer), include outer class users.
    parts = qual.split(".")
    for i in range(1, len(parts)):
        users |= maps.class_users(f"{mod}:{'.'.join(parts[:i])}")
    for ckey, (_, mro) in maps.class_info.items():
        if key in mro:
            cmod, cqual = ckey.split(":", 1)
            refs.add(cqual.split(".", 1)[0])
            cparts = cqual.split(".")
            for i in range(1, len(cparts)):
                users |= maps.class_users(f"{cmod}:{'.'.join(cparts[:i])}")
    for r in refs:
        users |= maps.name_readers(r)
    return users


def _bits_for_cell(cell, detail, maps):
    """Bitset of tests that can observe a change to `cell`, or None to fall back to all importers."""
    kind, owner, attr = cell
    if "@" in owner:
        return None  # collision class (e.g. dialect-generated Tokenizer@anchor)
    if attr in _DISALLOWED_SYNTHETIC:
        return None
    if _is_dunder(attr) and attr not in _ALLOWED_DUNDERS:
        return None

    use_keys = (
        maps.has_cells
        and maps.cell_tracked(cell)
        and detail.get("kind") in ("dict", "set")
        and detail.get("keys") is not None
        and not detail.get("order")
        and not detail.get("unstable")
        and not detail.get("added")
        and not detail.get("removed")
    )

    if kind == "c":
        mod_path = _module_to_path(owner.split(":", 1)[0], maps)
        if mod_path is None:
            return None
        imp_bits = maps.imported(mod_path)
        users = _class_family_users(owner, maps)
        if users is None:
            return None
        if use_keys:
            key_bits = 0
            for kf in detail["keys"]:
                key_bits |= maps.key_readers(cell, kf)
            whole_bits = users & (maps.whole_readers(attr) | maps.dyn_name_readers(attr, family_key=owner))
            return imp_bits & (key_bits | whole_bits)
        dyn = maps.dyn_name_readers(attr, family_key=owner) if maps.has_cells else maps.dynamic_readers(
            attr, family_key=owner)
        return imp_bits & users & (maps.name_readers(attr) | dyn)

    if kind == "g":
        mod_path = _module_to_path(owner, maps)
        if mod_path is None:
            return None
        imp_bits = maps.imported(mod_path)
        if use_keys:
            key_bits = 0
            for kf in detail["keys"]:
                key_bits |= maps.key_readers(cell, kf)
            whole_bits = imp_bits & (maps.whole_readers(attr) | maps.dyn_name_readers(attr, family_key=None))
            return key_bits | whole_bits
        dyn = maps.dyn_name_readers(attr, family_key=None) if maps.has_cells else maps.dynamic_readers(
            attr, family_key=None)
        return imp_bits & (maps.name_readers(attr) | dyn)

    return None


def resolve_cells(res, maps, src_root, state_probe):
    """Resolve `res.cell_sites` (list of (path, site, fallback_bits)) using `state_probe`.

    Returns a verdict note string and adds either the narrowed bits or fallback bits to `res`.
    """
    fallback = 0
    for _, _, fb in res.cell_sites:
        fallback |= fb

    if not fallback & ~res.bits:
        for _, site, fb in res.cell_sites:
            res.add(fb, None)
        return "cells narrowing skipped: falling back adds no test"

    why = None
    if res.probe_blockers:
        why = f"probe blocked ({res.probe_blockers[0]})"
    elif state_probe is None:
        why = "no state probe available"
    elif not maps.has_class_data:
        why = "maps lack class data"
    else:
        for path, site, _ in res.cell_sites:
            if not site.state_ok:
                why = f"{path}: {site.state_why}"
                break
            cprob = _callee_problem(site.state_callees, res.patched, src_root)
            if cprob:
                why = f"{path}: {cprob}"
                break

    if why is None:
        res.probe_calls += 1
        try:
            diff = state_probe({p: src for p, src in res.patched.items() if p.startswith("sqlglot/")})
        except Exception as error:  # noqa: BLE001
            diff = None
            why = f"state probe error ({error!r})"
        if why is None:
            if diff is None or not diff.ok:
                why = f"state probe failed ({getattr(diff, 'why', 'none')})"
            elif diff.env_changed:
                why = f"env changed ({diff.env_changed[0]})"
            elif diff.new_modules or diff.lost_modules:
                why = "sqlglot module set changed"
            elif diff.opaque:
                why = f"opaque value ({diff.opaque[0]})"
            elif diff.warm_caches:
                why = f"warm cache ({diff.warm_caches[0]})"
            else:
                narrow = maps.cells_untrusted if maps.has_cells else 0
                patched_mods = {p[:-3].replace("/", ".") for p in res.patched if p.endswith(".py")}
                for cell, detail in diff.cells.items():
                    if detail.get("unstable"):
                        owner_mod = cell[1].split(":", 1)[0]
                        if owner_mod not in patched_mods:
                            continue
                    cb = _bits_for_cell(cell, detail, maps)
                    if cb is None:
                        why = f"unsupported differing cell {cell}"
                        break
                    narrow |= cb
                if why is None:
                    res.add(narrow & fallback)
                    stable_n = sum(1 for d in diff.cells.values() if not d.get("unstable"))
                    return f"cells narrowing kept: {stable_n} differing cell(s)"

    for path, site, fb in res.cell_sites:
        res.add(fb, f"{path}: {site}; all importers")
    return f"cells narrowing dropped: {why}"
