"""Loads the dependency maps and answers "which tests depend on X" as bitsets."""

import marshal
import zlib


class Maps:
    def __init__(self, path):
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
