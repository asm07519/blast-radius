"""Loads the dependency maps and answers "which tests depend on X" as bitsets."""

import marshal
import os
import zlib


class Maps:
    def __init__(self, path, cells_path=None):
        with open(path, "rb") as fh:
            data = marshal.loads(zlib.decompress(fh.read()))
        self.ids = data["ids"]
        self.files = data["files"]
        self.file_index = {f: i for i, f in enumerate(self.files)}
        self._call = data["call"]
        self._import = data["import"]
        self._imported = data["imported"]
        self._reads = data["reads"]
        self._reads_lc = data["reads_lc"]
        self._listed = data["listed"]
        self.sets = data["sets"]
        self.mapped = data["mapped"]
        self.untrusted = data["untrusted"]
        self._class_users = data.get("class_users", {})
        self.classes_at_import = frozenset(data.get("classes_at_import", ()))
        self.names = data.get("names", {})
        self.dynamic = data.get("dynamic", [])
        self.class_info = data.get("class_info", {})
        self.has_class_data = bool(self._class_users) and bool(self.class_info) and bool(self.names)
        self.typeprof = data.get("typeprof", {})
        self.exec_phases = {}
        for test_id, info in data.get("exec_phases", {}).items():
            self.exec_phases[test_id] = {
                "suite": info["suite"], "fingerprint": info["fingerprint"],
                "exposed": {f: frozenset(v) for f, v in info["exposed"].items()},
            }
        self.subtests = {}
        for test_id, info in data.get("subtests", {}).items():
            self.subtests[test_id] = {
                "outside": {f: frozenset(v) for f, v in info["outside"].items()},
                "windows": [(value, {f: frozenset(v) for f, v in files.items()}) for value, files in info["windows"]],
            }
        self.typeprof_incomplete = data.get("typeprof_incomplete", None)
        self._load_cells(path, cells_path)

    def _load_cells(self, maps_path, cells_path):
        self.has_cells = False
        self.cells_untrusted = 0
        self._cells_sets = ()
        self._cells_tracked = frozenset()
        self._cells_keys = {}  # cell -> {key_fp: set_idx}
        self._cells_any_key = {}  # cell -> set_idx
        self._cells_whole = {}  # name -> set_idx
        self._cells_dyn = {}  # (family_or_empty, name) -> set_idx
        self._cells_dyn_star = {}  # family_or_empty -> set_idx
        if cells_path is None:
            cells_path = os.path.join(os.path.dirname(os.path.abspath(str(maps_path))), "cells.bin")
        if not os.path.isfile(cells_path):
            return
        with open(cells_path, "rb") as fh:
            cdata = marshal.loads(zlib.decompress(fh.read()))
        if cdata.get("ids") != self.ids:
            return
        self._cells_sets = cdata["sets"]
        self._cells_tracked = frozenset(cdata.get("tracked", ()))
        self._cells_keys = cdata.get("keys", {})
        self._cells_any_key = cdata.get("any_key", {})
        self._cells_whole = cdata.get("whole", {})
        self._cells_dyn = cdata.get("dyn", {})
        self._cells_dyn_star = cdata.get("dyn_star", {})
        self.cells_untrusted = cdata.get("untrusted", 0)
        self.has_cells = True

    def cell_tracked(self, cell):
        return cell in self._cells_tracked

    def key_readers(self, cell, key_fp):
        per = self._cells_keys.get(cell)
        if not per:
            return 0
        s = per.get(key_fp)
        return self._cells_sets[s] if s is not None else 0

    def any_key_readers(self, cell):
        s = self._cells_any_key.get(cell)
        return self._cells_sets[s] if s is not None else 0

    def whole_readers(self, name):
        s = self._cells_whole.get(name)
        return self._cells_sets[s] if s is not None else 0

    def dyn_name_readers(self, name, family_key=None):
        if not self.has_cells:
            return self.dynamic_readers(name, family_key=family_key)
        sets = self._cells_sets
        bits = 0
        # Traced dynamic reads: exact name or wildcard "*" (vars/dir/getmembers), per family or global.
        keys = [""]
        if family_key is not None:
            keys.append(family_key)
            # Subclasses/superclasses sharing an MRO with family_key
            kmro = self.class_info.get(family_key, (False, ()))[1]
            keys.extend(kmro)
        for fk in keys:
            s = self._cells_dyn.get((fk, name))
            if s is not None:
                bits |= sets[s]
            s = self._cells_dyn_star.get(fk)
            if s is not None:
                bits |= sets[s]
        return bits

    def _lines(self, table, path, lines):
        k = self.file_index.get(path)
        if k is None:
            return 0
        per = table.get(k)
        if not per:
            return 0
        sets = self.sets
        bits = 0
        for line in lines:
            s = per.get(line)
            if s is not None:
                bits |= sets[s]
        return bits

    def call(self, path, lines):
        """Tests that ran any of these lines inside a function, lambda or generator."""
        return self._lines(self._call, path, lines)

    def import_lines(self, path, lines):
        """Tests that ran any of these lines in module or class-body code."""
        return self._lines(self._import, path, lines)

    def imported(self, path):
        """Tests whose process executed this file's module code (imported it)."""
        k = self.file_index.get(path)
        if k is None:
            return 0
        s = self._imported.get(k)
        return self.sets[s] if s is not None else 0

    def reads(self, path, linecache=True):
        bits = 0
        s = self._reads.get(path)
        if s is not None:
            bits |= self.sets[s]
        if linecache:
            s = self._reads_lc.get(path)
            if s is not None:
                bits |= self.sets[s]
        return bits

    def listers(self, directory):
        bits = 0
        d = directory.rstrip("/")
        while True:
            s = self._listed.get(d)
            if s is not None:
                bits |= self.sets[s]
            if not d:
                break
            d = d.rsplit("/", 1)[0] if "/" in d else ""
        return bits

    def file_known(self, path):
        return path in self.file_index

    def class_users(self, key):
        """Tests that used an object or classmethod of this class or a subclass."""
        s = self._class_users.get(key)
        return self.sets[s] if s is not None else 0

    def name_readers(self, name):
        rec = self.names.get(name)
        return self.sets[rec[0]] if rec else 0

    def dynamic_readers(self, name, family_key=None):
        """Tests running code that may read attribute `name` through a computed name.

        With family_key (a class attribute of that class is being read), sites whose
        object is a module are skipped, and receiver-based sites count only when their
        class is related to that class.
        """
        import re

        bits = 0
        for entry in self.dynamic:
            pattern, s = entry[0], entry[1]
            kind = entry[2] if len(entry) > 2 else "other"
            encl = entry[3] if len(entry) > 3 else ""
            if pattern is not None and not re.match(pattern, name):
                continue
            if family_key is not None:
                if kind == "module":
                    continue
                if kind == "self" and encl:
                    emro = self.class_info.get(encl, (False, ()))[1]
                    kmro = self.class_info.get(family_key, (False, ()))[1]
                    if family_key not in emro and encl not in kmro:
                        continue
            bits |= self.sets[s]
        return bits

    def swap_is_noop(self, path, proof):
        """True when swapping is/== (or is not/!=) at this comparison cannot change any test.

        Every operand-type pair profiled there must make `==` agree with identity. Tests
        whose profiling run stopped early are not covered by this and must be added by the caller.
        """
        if self.typeprof_incomplete is None:
            return False
        line, col, end_line = proof[0], proof[1], proof[2]
        rows = self.typeprof.get(f"{path}:{line}:{col}")
        if rows is None:
            return False  # never reached while profiling: rely on coverage instead
        return all(_pair_safe(lc, rc, same) for lc, rc, same in rows)


def _pair_safe(lc, rc, same):
    plain = ("ident", "enum", "builtin", "nonesafe")
    if lc == "none" and rc == "none":
        return True
    if lc == "none":
        return rc in plain
    if rc == "none":
        return lc in plain
    if same:
        return lc in ("ident", "enum")
    return lc == "ident" and rc == "ident"
