"""Unified diff parsing and application, on bytes.

parse(data) returns a list of FilePatch. apply(fp, old) returns the new bytes or
raises PatchError; callers treat a patch they cannot apply as "unknown change"
and fall back to something safe.
"""

import re

_HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


class Hunk:
    __slots__ = ("old_start", "old_len", "new_start", "new_len", "lines")

    def __init__(self, old_start, old_len, new_start, new_len):
        self.old_start = old_start
        self.old_len = old_len
        self.new_start = new_start
        self.new_len = new_len
        self.lines = []  # (tag, content bytes incl. newline); tag in b" -+"


class FilePatch:
    def __init__(self):
        self.old_path = None  # None for a new file
        self.new_path = None  # None for a deleted file
        self.hunks = []
        self.binary = False
        self.mode_only = False
        self.header = []

    @property
    def path(self):
        return self.new_path or self.old_path

    def __repr__(self):
        return f"<FilePatch {self.old_path} -> {self.new_path} hunks={len(self.hunks)} bin={self.binary}>"


def _strip(path):
    path = path.strip()
    if b"\t" in path:
        path = path.split(b"\t", 1)[0]
    if path.startswith(b'"') and path.endswith(b'"'):
        path = path[1:-1].decode("unicode_escape").encode("latin-1")
    if path == b"/dev/null":
        return None
    if path[:2] in (b"a/", b"b/"):
        path = path[2:]
    return path.decode("utf-8", "surrogateescape")


def parse(data):
    lines = data.splitlines(keepends=True)
    patches = []
    cur = None
    hunk = None
    remaining = [0, 0]
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if hunk is not None and (remaining[0] > 0 or remaining[1] > 0):
            tag = line[:1]
            if tag in (b" ", b"-", b"+"):
                hunk.lines.append((tag, line[1:]))
                if tag != b"+":
                    remaining[0] -= 1
                if tag != b"-":
                    remaining[1] -= 1
                i += 1
                continue
            if tag == b"\\":
                _no_newline(hunk)
                i += 1
                continue
            if line in (b"\n", b"\r\n"):
                # Some tools drop the leading space of an empty context line.
                hunk.lines.append((b" ", line))
                remaining[0] -= 1
                remaining[1] -= 1
                i += 1
                continue
            hunk = None
        if line.startswith(b"\\") and hunk is not None:
            _no_newline(hunk)
            i += 1
            continue
        if line.startswith(b"diff --git "):
            cur = FilePatch()
            patches.append(cur)
            hunk = None
            rest = line[len(b"diff --git "):].rstrip(b"\r\n")
            m = re.match(rb"(\"?a/.*?\"?) (\"?b/.*\"?)$", rest)
            if m:
                cur.old_path = _strip(m.group(1))
                cur.new_path = _strip(m.group(2))
            i += 1
            continue
        if line.startswith(b"--- ") and i + 1 < n and lines[i + 1].startswith(b"+++ "):
            if cur is None or cur.hunks:
                cur = FilePatch()
                patches.append(cur)
            cur.old_path = _strip(line[4:])
            cur.new_path = _strip(lines[i + 1][4:])
            hunk = None
            i += 2
            continue
        m = _HUNK.match(line)
        if m and cur is not None:
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            hunk = Hunk(old_start, old_len, new_start, new_len)
            cur.hunks.append(hunk)
            remaining = [old_len, new_len]
            i += 1
            continue
        if cur is not None:
            cur.header.append(line)
            if line.startswith(b"new file mode"):
                cur.old_path = None
            elif line.startswith(b"deleted file mode"):
                cur.new_path = None
            elif line.startswith(b"rename from "):
                cur.old_path = _strip(line[len(b"rename from "):])
            elif line.startswith(b"rename to "):
                cur.new_path = _strip(line[len(b"rename to "):])
            elif line.startswith(b"copy from "):
                cur.old_path = _strip(line[len(b"copy from "):])
            elif line.startswith(b"copy to "):
                cur.new_path = _strip(line[len(b"copy to "):])
            elif line.startswith(b"Binary files") or line.startswith(b"GIT binary patch"):
                cur.binary = True
        i += 1
    for fp in patches:
        is_copy = any(h.startswith(b"copy from") for h in fp.header)
        if is_copy:
            fp.copy = True
        if not fp.hunks and not fp.binary and fp.old_path == fp.new_path:
            fp.mode_only = True
    return patches


def _no_newline(hunk):
    if hunk.lines:
        tag, content = hunk.lines[-1]
        if content.endswith(b"\r\n"):
            content = content[:-2]
        elif content.endswith(b"\n"):
            content = content[:-1]
        hunk.lines[-1] = (tag, content)


def apply(fp, old):
    """Apply a text FilePatch to old bytes (b"" for a new file)."""
    if fp.binary:
        raise PatchError("binary")
    if fp.new_path is None:
        return None
    src = old.splitlines(keepends=True) if old else []
    out = []
    pos = 0
    for h in fp.hunks:
        expected = [c for t, c in h.lines if t != b"+"]
        start = h.old_start - 1 if h.old_len > 0 else h.old_start
        at = _find(src, expected, max(start, 0), pos)
        if at is None:
            raise PatchError(f"hunk at {h.old_start} does not apply")
        out.extend(src[pos:at])
        out.extend(c for t, c in h.lines if t != b"-")
        pos = at + len(expected)
    out.extend(src[pos:])
    return b"".join(out)


def _find(src, expected, start, floor):
    def ok(at):
        if at < floor or at + len(expected) > len(src):
            return False
        return src[at:at + len(expected)] == expected

    if ok(start):
        return start
    for delta in range(1, 200):
        for at in (start - delta, start + delta):
            if ok(at):
                return at
    # Tolerate line-ending differences only as a last resort.
    norm = [e.rstrip(b"\r\n") for e in expected]
    for at in range(floor, len(src) - len(expected) + 1):
        if [s.rstrip(b"\r\n") for s in src[at:at + len(expected)]] == norm:
            return at
    return None


def changed_old_lines(fp):
    """Old-file line numbers removed or replaced, and old positions where lines are inserted.

    An insertion position p means new lines appear between old lines p-1 and p.
    """
    removed, inserted_before = set(), set()
    for h in fp.hunks:
        ln = h.old_start if h.old_len > 0 else h.old_start + 1
        for tag, _ in h.lines:
            if tag == b" ":
                ln += 1
            elif tag == b"-":
                removed.add(ln)
                ln += 1
            else:
                inserted_before.add(ln)
    return removed, inserted_before
