"""Filesystem overlay of the pinned tree for Mode 0 "fs" runs (stdlib only).

    root = build(src_root, changes, workdir)     # -> <workdir>/br-ov-XXXXXX/<basename(src_root)>
    fields = spec_fields(root)                   # {"overlay": "fs", "src": root, "patched": {},
                                                 #  "shadowed": [...], "pinned": src_root}
    ...                                          # mode0_runner.execute(dict(spec, **fields), ...)
    remove(root)

Layout. The overlay root mirrors the top level of src_root:
  * MIRROR_DIRS (sqlglot/, tests/) and any other top-level directory that holds a changed file
    are REAL directories at every level, so os.walk / glob / Path.rglob / pkgutil.iter_modules /
    os.listdir see exactly the entries of the pinned tree (a directory symlink would stop
    walks that do not follow links, and hide files from a failing test).
  * every file in them is a symlink to the pinned file (absolute target), except
  * each changed file, which is a regular file holding the new bytes (mode copied), and
  * the bytecode of each changed module: <dir>/__pycache__/<stem>.*.pyc is NOT mirrored.
    Import validates timestamp pycs against stat() of the source, which follows the symlink of
    an unchanged module (pinned mtime and size: the pinned pyc stays valid and imports stay
    fast). For a changed module a mirrored pyc could be used anyway: an unchecked-hash pyc is
    never validated, and a timestamp pyc is accepted when mtime and size happen to match; -B
    does not stop READING pycs. With no pyc the patched source is compiled (in memory: the
    runner never writes bytecode).
  * every other top-level entry is a plain symlink to the pinned entry.

The pinned originals of changed files ("shadowed") remain reachable through realpath(); the
runner's audit hook flags any test that opens one (read_shadowed).

Refusals raise OverlayError and leave nothing behind (the caller keeps the tests): a change
path that is absolute, has '..', '.', empty or NUL components, or crosses a symlink; deletions
(None) and new files (not supported yet); patched bytes over max_bytes; more than max_entries
entries; not enough free space or inodes in workdir; and anything unexpected in a mirrored tree
(symlinks, sockets, fifos, devices) or at the top level (dangling links, symlink loops).

remove() deletes the container only if it looks like one build() made (br-ov- prefix and a
manifest) and never follows a symlink: links are unlinked, never descended into.
"""

import json
import os
import stat
import tempfile
import time

MIRROR_DIRS = ("sqlglot", "tests")
PREFIX = "br-ov-"
MANIFEST = "manifest.json"
MAX_BYTES = 8 << 20          # patched bytes written as real files
MAX_ENTRIES = 50000          # directories + symlinks + files created
RESERVE_BYTES = 4 << 20      # free space that must remain in workdir afterwards
PAGE = 4096
MAX_DEPTH = 64


class OverlayError(Exception):
    """The overlay cannot be built (or removed) safely; the caller must keep the tests."""


def _check_rel(rel):
    if not isinstance(rel, str) or not rel or "\0" in rel or "\\" in rel or rel.startswith("/"):
        raise OverlayError(f"bad change path {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise OverlayError(f"bad change path {rel!r}")
    return parts


def _changes(src, changes):
    """{rel: bytes} after validation against the pinned tree."""
    if not isinstance(changes, dict):
        raise OverlayError("changes must be a dict")
    out = {}
    for rel, data in changes.items():
        parts = _check_rel(rel)
        if data is None:
            raise OverlayError(f"deleting {rel} is not supported yet")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise OverlayError(f"content of {rel} must be bytes")
        path = src
        for k, part in enumerate(parts):
            path = os.path.join(path, part)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                raise OverlayError(f"adding {rel} is not supported yet") from None
            except OSError as error:
                raise OverlayError(f"cannot stat {rel}: {error}") from None
            last = k == len(parts) - 1
            if stat.S_ISLNK(st.st_mode):
                raise OverlayError(f"change path {rel} crosses a symlink")
            if not last and not stat.S_ISDIR(st.st_mode):
                raise OverlayError(f"change path {rel}: {part} is not a directory")
            if last and not stat.S_ISREG(st.st_mode):
                raise OverlayError(f"change path {rel} is not a regular file")
        out[rel] = (bytes(data), stat.S_IMODE(st.st_mode))
    return out


class _Plan:
    def __init__(self, src, changed, max_entries):
        self.src = src
        self.changed = changed
        self.max_entries = max_entries
        self.ops = []           # ("dir", rel) | ("link", rel, target) | ("file", rel)
        self.long_links = 0
        # dir rel -> stems of changed .py files there (their pycs are not mirrored)
        self.stems = {}
        for rel in changed:
            d, _, name = rel.rpartition("/")
            if name.endswith(".py"):
                self.stems.setdefault(d, set()).add(name[:-3])
        self.skipped_pyc = []

    def add(self, op):
        self.ops.append(op)
        if len(self.ops) > self.max_entries:
            raise OverlayError(f"more than {self.max_entries} entries")
        if op[0] == "link" and len(os.fsencode(op[2])) >= 128:
            self.long_links += 1

    def tree(self, rel, depth=0):
        if depth > MAX_DEPTH:
            raise OverlayError(f"{rel}: deeper than {MAX_DEPTH}")
        self.add(("dir", rel))
        stems = None
        if os.path.basename(rel) == "__pycache__":
            stems = self.stems.get(os.path.dirname(rel))
        try:
            with os.scandir(os.path.join(self.src, rel)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as error:
            raise OverlayError(f"cannot list {rel}: {error}") from None
        for e in entries:
            sub = rel + "/" + e.name
            try:
                st = e.stat(follow_symlinks=False)
            except OSError as error:
                raise OverlayError(f"cannot stat {sub}: {error}") from None
            if stat.S_ISDIR(st.st_mode):
                self.tree(sub, depth + 1)
            elif stat.S_ISREG(st.st_mode):
                if sub in self.changed:
                    self.add(("file", sub))
                elif stems and e.name.endswith(".pyc") and e.name.split(".", 1)[0] in stems:
                    self.skipped_pyc.append(sub)
                else:
                    self.add(("link", sub, os.path.join(self.src, sub)))
            else:
                kind = "symlink" if stat.S_ISLNK(st.st_mode) else "special file"
                raise OverlayError(f"unexpected {kind} in mirrored tree: {sub}")

    def top(self, mirror):
        try:
            with os.scandir(self.src) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as error:
            raise OverlayError(f"cannot list {self.src}: {error}") from None
        names = {e.name for e in entries}
        for d in mirror:
            if d not in names:
                raise OverlayError(f"{d}/ missing from {self.src}")
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError as error:
                raise OverlayError(f"cannot stat {e.name}: {error}") from None
            if e.name in mirror:
                if not stat.S_ISDIR(st.st_mode):
                    raise OverlayError(f"{e.name} is not a real directory")
                self.tree(e.name)
                continue
            if e.name in self.changed:
                self.add(("file", e.name))
                continue
            if stat.S_ISLNK(st.st_mode):
                try:
                    os.stat(e.path)  # dangling links and loops (ELOOP) are refused
                except OSError as error:
                    raise OverlayError(f"bad top-level symlink {e.name}: {error}") from None
            elif not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                raise OverlayError(f"unexpected special file at top level: {e.name}")
            self.add(("link", e.name, e.path))

    def needed_bytes(self):
        dirs = sum(1 for op in self.ops if op[0] == "dir")
        data = sum(-(-len(self.changed[rel][0]) // PAGE) * PAGE for rel in self.changed)
        return data + (dirs + self.long_links + 1) * PAGE


def _space(workdir, plan, reserve):
    try:
        vfs = os.statvfs(workdir)
    except OSError as error:
        raise OverlayError(f"statvfs {workdir}: {error}") from None
    free = vfs.f_bavail * vfs.f_frsize
    need = plan.needed_bytes() + reserve
    if free < need:
        raise OverlayError(f"not enough space in {workdir}: {free} free, {need} needed")
    if vfs.f_files and vfs.f_favail < len(plan.ops) + 1024:
        raise OverlayError(f"not enough inodes in {workdir}: {vfs.f_favail} free")
    return free


def build(src_root, changes, workdir, *, max_bytes=MAX_BYTES, max_entries=MAX_ENTRIES,
          reserve=RESERVE_BYTES, mirror=MIRROR_DIRS):
    """Build the overlay; returns its root (absolute). Raises OverlayError (nothing left behind).

    changes: {repo-relative posix path: new bytes}. None (deletion) and paths that do not exist
    in src_root (additions) are refused for now.
    """
    cpu0 = time.process_time()
    src = os.path.realpath(src_root)
    if not os.path.isdir(src):
        raise OverlayError(f"{src_root} is not a directory")
    changed = _changes(src, changes)
    total = sum(len(data) for data, _ in changed.values())
    if total > max_bytes:
        raise OverlayError(f"patched bytes {total} exceed {max_bytes}")
    mirror = set(mirror)
    for rel in changed:
        if "/" in rel:
            mirror.add(rel.split("/", 1)[0])  # a changed file deep in another top-level dir
    workdir = os.path.abspath(workdir)
    real_wd = os.path.realpath(workdir)
    if real_wd == src or real_wd.startswith(src + os.sep):
        raise OverlayError(f"workdir {workdir} is inside {src}")
    plan = _Plan(src, changed, max_entries)
    plan.top(sorted(mirror))
    _space(workdir, plan, reserve)

    try:
        container = tempfile.mkdtemp(prefix=PREFIX, dir=workdir)
    except OSError as error:
        raise OverlayError(f"cannot create overlay in {workdir}: {error}") from None
    root = os.path.join(container, os.path.basename(src) or "src")
    try:
        shadowed = sorted(os.path.join(src, rel) for rel in changed)
        manifest = {"pinned": src, "root": root, "changed": sorted(changed),
                    "shadowed": shadowed, "skipped_pyc": plan.skipped_pyc,
                    "entries": len(plan.ops), "bytes": total}
        _write(os.path.join(container, MANIFEST), json.dumps(manifest).encode(), 0o644)
        os.mkdir(root, 0o755)
        mkdir, symlink, join = os.mkdir, os.symlink, os.path.join
        for op in plan.ops:
            if op[0] == "link":
                symlink(op[2], join(root, op[1]))
            elif op[0] == "dir":
                mkdir(join(root, op[1]), 0o755)
            else:
                data, mode = changed[op[1]]
                _write(join(root, op[1]), data, mode)
    except BaseException as error:
        _rmtree(container)
        if isinstance(error, OSError):
            raise OverlayError(f"building overlay: {error}") from None
        raise
    manifest["build_cpu"] = round(time.process_time() - cpu0, 6)
    try:
        _write(os.path.join(container, MANIFEST), json.dumps(manifest).encode(), 0o644, replace=True)
    except OSError:
        pass
    return root


def _write(path, data, mode, replace=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if replace else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fchmod(fd, mode & 0o777)
    finally:
        os.close(fd)


def _container(overlay_root):
    root = os.path.abspath(overlay_root)
    container = os.path.dirname(root)
    try:
        st_root = os.lstat(root)
        st_man = os.lstat(os.path.join(container, MANIFEST))
        st_con = os.lstat(container)
    except OSError as error:
        raise OverlayError(f"not an overlay: {overlay_root} ({error})") from None
    if (not os.path.basename(container).startswith(PREFIX) or not stat.S_ISDIR(st_root.st_mode)
            or not stat.S_ISDIR(st_con.st_mode) or not stat.S_ISREG(st_man.st_mode)):
        raise OverlayError(f"not an overlay: {overlay_root}")
    return root, container


def info(overlay_root):
    """The manifest: pinned, root, changed, shadowed, skipped_pyc, entries, bytes, build_cpu."""
    _, container = _container(overlay_root)
    with open(os.path.join(container, MANIFEST), "rb") as fh:
        return json.loads(fh.read())


def spec_fields(overlay_root):
    """The Mode 0 spec fields for an fs run in this overlay."""
    m = info(overlay_root)
    return {"overlay": "fs", "src": m["root"], "patched": {}, "shadowed": m["shadowed"],
            "pinned": m["pinned"]}


def remove(overlay_root):
    """Delete the overlay (its whole br-ov- container). Never follows symlinks."""
    _, container = _container(overlay_root)
    _rmtree(container)


def _rmtree(top):
    """Remove `top` (a real directory) and everything below it without following any link."""
    try:
        st = os.lstat(top)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        raise OverlayError(f"refusing to remove {top}: not a real directory")
    stack = [(top, False)]
    while stack:
        path, listed = stack.pop()
        if listed:
            os.rmdir(path)
            continue
        stack.append((path, True))
        with os.scandir(path) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, False))
                else:
                    os.unlink(e.path)
