#!/usr/bin/env python3
"""Check the committed evidence (maps/maps.bin + maps/tests.json) against maps/manifest.json.

    python3 tools/validate_evidence.py --maps maps/maps.bin --tests maps/tests.json \
        --manifest maps/manifest.json [--repo SRC] [--root .] [--check-python] [--allow-untrusted]
    python3 tools/validate_evidence.py ... --repo SRC --write      # regenerate the manifest

Stdlib only. Everything in the "verified" part of the manifest is recomputed and a
mismatch exits 1: file digests and sizes, the test-catalog identity, the map format and
pinned commit, trust counts, one checksum per top-level map section, the pinned source
tree digest (with --repo), the base image and requirements.lock digests (when the
Dockerfiles / lock file are found under --root) and the Python version (--check-python).
Also exit 1 on untrusted or unmapped tests (unless --allow-untrusted), on an unknown
top-level map key, a broken internal reference, or anything that looks like evaluation
data (answers, failing tests, labels, mutants, patches). The "provenance" part (seeds,
commands, generator source digests) is reported, never fatal.

Section checksums are over the meaning of a section, not its bytes: set and file
indices are resolved and unordered tables are sorted, so re-interning or reordering
does not change them, while any changed test set, line or value does.
"""

import argparse
import hashlib
import json
import marshal
import os
import platform
import re
import sys
import zlib

MANIFEST_VERSION = 1
SQLGLOT_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
REPOSITORY = "tobymao/sqlglot"
# Every top-level key tools/compact_maps.py writes; selector/maps.py needs the first group.
REQUIRED = ("format", "commit", "ids", "files", "call", "import", "imported", "reads", "reads_lc", "listed",
            "mapped", "untrusted", "sets")
OPTIONAL = ("exec_phases", "subtests", "typeprof", "typeprof_incomplete", "class_users", "classes_at_import",
            "call_at_import", "names", "dynamic", "class_info")
KNOWN = REQUIRED + OPTIONAL
EXEC_KEYS = {"suite", "fingerprint", "exposed"}
SUITE_KEYS = {"test", "fixture", "schema", "tables"}
QUERY_KEYS = {"skip", "parse", "tables", "sta", "optimized", "rendered", "duck", "error"}
SUBTEST_KEYS = {"outside", "windows"}
# Dict keys that only evaluation data would use (sqlglot itself has `label` and `patch` names).
EVAL_KEYS = {"answer", "answers", "failing", "failing_tests", "failures", "labels", "mutant", "mutants",
             "patches", "patch_id", "patch_ids", "verdict", "verdicts", "expected_failures"}
# Strings that point at evaluation inputs.
EVAL_TEXT = re.compile(r"answers\.json|/data/commits|\.patch$|\bmutants?\b|truth-|sample/commits", re.I)
# Files whose digests describe the generator (provenance only).
GENERATOR = ("tools/trace/br_tracer.py", "tools/trace/br_plugin.py", "tools/trace/sitecustomize.py",
             "tools/typeprof/br_typeprof.py", "tools/typeprof/sitecustomize.py", "tools/build_maps.py",
             "tools/compact_maps.py", "tools/build_typeprof.py", "tools/subtest_cov.py", "tools/exec_phases.py",
             "tools/static_index.py", "tools/class_info.py", "tools/ground_truth.py", "selector/exec_fingerprint.py")
PROVENANCE = {
    "make_target": "make maps",
    "image": "tools/Dockerfile.tools (labelling environment: requirements.lock, sqlglot at the pinned commit)",
    "commands": [
        "build_maps.py --repo /src/sqlglot --tests maps/tests.json --work /work/maps -j JOBS --seed 0 --seed 1",
        "static_index.py --repo /src/sqlglot",
        "class_info.py --repo /src/sqlglot",
        "build_typeprof.py --repo /src/sqlglot --tests maps/tests.json -j JOBS",
        "subtest_cov.py --repo /src/sqlglot --test tests/test_generator.py::TestGenerator::test_generator_memory",
        "exec_phases.py --repo /src/sqlglot",
        "compact_maps.py --tests maps/tests.json --raw /work/maps/raw --static --classes --typeprof --subtests "
        "--exec-phases",
        "validate_evidence.py --repo /src/sqlglot --write",
    ],
    "seeds": {"build_maps": [0, 1], "build_typeprof": 0, "exec_phases": 0, "subtest_cov": "PYTHONHASHSEED=0",
              "static_index": "PYTHONHASHSEED=0", "class_info": "PYTHONHASHSEED=0"},
    "inputs": "pinned sqlglot source and maps/tests.json only; no answers, labels, patches or mutants",
}


class Invalid(Exception):
    pass


def sha256_bytes(blob):
    return hashlib.sha256(blob).hexdigest()


def sha256_file(path):
    with open(path, "rb") as fh:
        return sha256_bytes(fh.read())


def digest(obj):
    return sha256_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode())


def popcount(bits):
    return bin(bits).count("1") if bits else 0


def tree_digest(root):
    """Digest of every file (path, bytes) and symlink (path, target) under root; caches excluded."""
    entries = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d in ("__pycache__", ".pytest_cache", ".git"):
                continue
            if os.path.islink(full):
                entries.append((os.path.relpath(full, root), "l", os.readlink(full)))
            else:
                keep.append(d)
        dirnames[:] = sorted(keep)
        for f in filenames:
            if f.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root)
            if os.path.islink(full):
                entries.append((rel, "l", os.readlink(full)))
            else:
                entries.append((rel, "f", sha256_file(full)))
    entries.sort()
    h = hashlib.sha256()
    for rel, kind, value in entries:
        h.update(f"{kind} {value} {rel}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest(), len(entries)


def load_maps(path):
    with open(path, "rb") as fh:
        blob = fh.read()
    try:
        data = marshal.loads(zlib.decompress(blob))
    except Exception as error:
        raise Invalid(f"{path}: not a zlib-compressed marshal map ({error!r})")
    if not isinstance(data, dict):
        raise Invalid(f"{path}: top level is {type(data).__name__}, not a dict")
    return blob, data


def catalog(tests_blob):
    tests = json.loads(tests_blob)
    ids = [t["id"] for t in tests["tests"]]
    if [t["number"] for t in tests["tests"]] != list(range(len(ids))):
        raise Invalid("tests.json: numbers are not 0..n-1 in order")
    return tests, ids


# --- section checksums --------------------------------------------------------------------

def _hex(bits):
    return format(bits, "x") if isinstance(bits, int) else None


def canonical_sections(data):
    """Order-independent, index-free form of every top-level section (see module doc)."""
    sets, files = data["sets"], data["files"]

    def s(i):
        return _hex(sets[i])

    def f(i):
        return files[i]

    def lines_by_file(table):
        return sorted([f(k), line, s(i)] for k, per in table.items() for line, i in per.items())

    def files_lines(d):
        return sorted([path, sorted(v)] for path, v in d.items())

    out = {}
    for key, value in data.items():
        if key in ("format", "commit"):
            c = value
        elif key == "ids":
            c = list(value)
        elif key == "files":
            c = sorted(value)
        elif key == "sets":
            c = sorted(_hex(b) for b in value)
        elif key in ("call", "import"):
            c = lines_by_file(value)
        elif key == "imported":
            c = sorted([f(k), s(i)] for k, i in value.items())
        elif key in ("reads", "reads_lc", "listed", "class_users"):
            c = sorted([p, s(i)] for p, i in value.items())
        elif key in ("mapped", "untrusted", "typeprof_incomplete"):
            c = _hex(value)
        elif key == "classes_at_import":
            c = sorted(value)
        elif key == "call_at_import":
            c = sorted([f(k), line] for k, line in value)
        elif key == "names":
            c = sorted([n, s(r), sorted(map(list, imps)), sorted(map(list, calls))]
                       for n, (r, imps, calls) in value.items())
        elif key == "dynamic":
            c = sorted([e[0] or "", s(e[1]), *map(str, e[2:])] for e in value)
        elif key == "class_info":
            c = sorted([k, bool(t), list(mro)] for k, (t, mro) in value.items())
        elif key == "typeprof":
            c = sorted([site, sorted(map(list, rows))] for site, rows in value.items())
        elif key == "subtests":
            c = sorted([t, files_lines(info["outside"]), [[str(v), files_lines(fl)] for v, fl in info["windows"]]]
                       for t, info in value.items())
        elif key == "exec_phases":
            c = sorted([t, info["suite"], info["fingerprint"], files_lines(info["exposed"])]
                       for t, info in value.items())
        else:
            c = repr(value)
        out[key] = c
    return out


def section_checksums(data):
    return {k: digest(v) for k, v in sorted(canonical_sections(data).items())}


# --- structure and evaluation-data checks --------------------------------------------------

def check_structure(data, ids):
    problems = []
    unknown = sorted(set(data) - set(KNOWN))
    if unknown:
        problems.append(f"unknown top-level map keys: {unknown}")
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        problems.append(f"missing top-level map keys: {missing}")
        return problems
    nsets, nfiles, idset = len(data["sets"]), len(data["files"]), set(ids)

    def bad_set(i):
        return not isinstance(i, int) or not 0 <= i < nsets

    for key in ("call", "import"):
        for k, per in data[key].items():
            if not 0 <= k < nfiles or any(bad_set(i) for i in per.values()):
                problems.append(f"{key}: bad file or set index for file {k}")
                break
    for key in ("imported",):
        if any(not 0 <= k < nfiles or bad_set(i) for k, i in data[key].items()):
            problems.append(f"{key}: bad file or set index")
    for key in ("reads", "reads_lc", "listed", "class_users"):
        if any(bad_set(i) for i in data.get(key, {}).values()):
            problems.append(f"{key}: bad set index")
    if any(bad_set(r) for r, _, _ in data.get("names", {}).values()):
        problems.append("names: bad set index")
    if any(bad_set(e[1]) for e in data.get("dynamic", [])):
        problems.append("dynamic: bad set index")
    if any(not 0 <= k < nfiles for k, _ in data.get("call_at_import", ())):
        problems.append("call_at_import: bad file index")
    limit = 1 << len(ids)
    for key in ("mapped", "untrusted", "typeprof_incomplete"):
        v = data.get(key)
        if v is not None and (not isinstance(v, int) or v < 0 or v >= limit):
            problems.append(f"{key}: not a bitset over the {len(ids)} tests")
    for test, info in data.get("exec_phases", {}).items():
        if test not in idset:
            problems.append(f"exec_phases: unknown test {test!r}")
        if set(info) != EXEC_KEYS or not set(info["suite"]) <= SUITE_KEYS or set(info["fingerprint"]) != {"queries"}:
            problems.append(f"exec_phases[{test!r}]: unexpected keys")
        elif any(not set(q) <= QUERY_KEYS for q in info["fingerprint"]["queries"]):
            problems.append(f"exec_phases[{test!r}]: unexpected query keys")
    for test, info in data.get("subtests", {}).items():
        if test not in idset:
            problems.append(f"subtests: unknown test {test!r}")
        if set(info) != SUBTEST_KEYS:
            problems.append(f"subtests[{test!r}]: unexpected keys")
    problems += evaluation_data(data)
    return problems


def evaluation_data(data):
    """Keys or strings anywhere in the maps that only evaluation data would contain."""
    found = []

    def walk(x, where):
        if len(found) > 20:
            return
        if isinstance(x, str):
            if EVAL_TEXT.search(x):
                found.append(f"{where}: evaluation-looking string {x[:60]!r}")
        elif isinstance(x, dict):
            for k, v in x.items():
                if isinstance(k, str) and k.lower() in EVAL_KEYS:
                    found.append(f"{where}: evaluation-looking key {k!r}")
                walk(k, where)
                walk(v, where)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v, where)

    for key, value in data.items():
        if key.lower() in EVAL_KEYS:
            found.append(f"top level: evaluation-looking key {key!r}")
        walk(value, key)
    return found


# --- manifest ------------------------------------------------------------------------------

def counts(data, ids):
    n = len(ids)
    full = (1 << n) - 1
    mapped, bad = data["mapped"] & full, data["untrusted"] & full
    tp = data.get("typeprof_incomplete")
    return {
        "ids": n,
        "mapped": popcount(mapped),
        "unmapped": n - popcount(mapped),
        "trusted": popcount(mapped & ~bad),
        "untrusted": popcount(bad),
        "typeprof_incomplete": None if tp is None else popcount(tp),
        "typeprof_sites": len(data.get("typeprof", {})),
        "subtests": {t: len(v["windows"]) for t, v in sorted(data.get("subtests", {}).items())},
        "exec_phases": {t: len(v["fingerprint"]["queries"]) for t, v in sorted(data.get("exec_phases", {}).items())},
        "files": len(data["files"]),
        "call_lines": sum(map(len, data["call"].values())),
        "import_lines": sum(map(len, data["import"].values())),
        "sets": len(data["sets"]),
    }


def base_image(path):
    with open(path) as fh:
        for line in fh:
            m = re.match(r"\s*FROM\s+(\S+)", line, re.I)
            if m:
                return m.group(1)
    return None


def compact_format(root):
    path = os.path.join(root, "tools", "compact_maps.py")
    if not os.path.exists(path):
        return None
    m = re.search(r"^FORMAT = (\d+)", open(path).read(), re.M)
    return int(m.group(1)) if m else None


def environment(root, python_version):
    env = {"python": python_version}
    for name, rel in (("base_image", "Dockerfile"), ("tools_base_image", os.path.join("tools", "Dockerfile.tools"))):
        p = os.path.join(root, rel)
        env[name] = base_image(p) if os.path.exists(p) else None
    lock = os.path.join(root, "requirements.lock")
    env["requirements_lock_sha256"] = sha256_file(lock) if os.path.exists(lock) else None
    return env


def generator_sources(root):
    out = {}
    for rel in GENERATOR:
        p = os.path.join(root, rel)
        out[rel] = sha256_file(p) if os.path.exists(p) else None
    return out


def build_manifest(maps_path, tests_path, repo, root, python_version, jobs=None):
    blob, data = load_maps(maps_path)
    with open(tests_path, "rb") as fh:
        tests_blob = fh.read()
    tests, ids = catalog(tests_blob)
    tree, nfiles = tree_digest(repo)
    gen = generator_sources(root)
    return {
        "manifest_version": MANIFEST_VERSION,
        "verified": {
            "source": {"repository": REPOSITORY, "commit": data.get("commit"), "tree_sha256": tree,
                       "tree_entries": nfiles},
            "maps": {"path": "maps/maps.bin", "sha256": sha256_bytes(blob), "size": len(blob),
                     "format": data.get("format")},
            "tests": {"path": "maps/tests.json", "sha256": sha256_bytes(tests_blob), "size": len(tests_blob),
                      "catalog_sha256": sha256_bytes("\n".join(ids).encode()), "ids": len(ids),
                      "skipped": len(tests.get("skipped", []))},
            "environment": environment(root, python_version),
            "counts": counts(data, ids),
            "sections": section_checksums(data),
        },
        "provenance": dict(PROVENANCE, jobs=jobs, generator_sha256={k: v for k, v in gen.items() if v},
                           tracer_sha256=digest({k: v for k, v in gen.items() if k.startswith("tools/trace/")})),
    }


def compare(expected, actual, prefix=""):
    """Paths where two JSON-like values differ (None in `actual` = could not be recomputed here)."""
    diffs = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k in sorted(set(expected) | set(actual)):
            diffs += compare(expected.get(k, "<absent>"), actual.get(k, "<absent>"), f"{prefix}{k}.")
    elif expected != actual:
        diffs.append((prefix.rstrip("."), expected, actual))
    return diffs


def validate(args):
    errors, notes = [], []
    blob, data = load_maps(args.maps)
    with open(args.tests, "rb") as fh:
        tests_blob = fh.read()
    tests, ids = catalog(tests_blob)
    try:
        manifest = json.load(open(args.manifest))
    except FileNotFoundError:
        raise Invalid(f"{args.manifest}: missing (run make manifest)")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        errors.append(f"manifest_version {manifest.get('manifest_version')} != {MANIFEST_VERSION}")
    exp = manifest.get("verified", {})

    if data.get("ids") != ids:
        errors.append("maps were built for a different tests.json (ids differ)")
    errors += check_structure(data, ids)

    got = {
        "maps": {"path": "maps/maps.bin", "sha256": sha256_bytes(blob), "size": len(blob), "format": data.get("format")},
        "tests": {"path": "maps/tests.json", "sha256": sha256_bytes(tests_blob), "size": len(tests_blob),
                  "catalog_sha256": sha256_bytes("\n".join(ids).encode()), "ids": len(ids),
                  "skipped": len(tests.get("skipped", []))},
        "counts": counts(data, ids) if not errors else exp.get("counts"),
        "sections": section_checksums(data) if not errors else exp.get("sections"),
    }
    src = dict(exp.get("source", {}), repository=REPOSITORY, commit=data.get("commit"))
    if args.repo:
        src["tree_sha256"], src["tree_entries"] = tree_digest(args.repo)
    else:
        notes.append("source tree digest not checked (no --repo)")
    got["source"] = src
    env = dict(exp.get("environment", {}))
    root_env = environment(args.root, platform.python_version())
    for k in ("base_image", "tools_base_image", "requirements_lock_sha256"):
        if root_env[k] is None:
            notes.append(f"environment.{k} not checked (file not under --root)")
        else:
            env[k] = root_env[k]
    if args.check_python:
        env["python"] = root_env["python"]
    else:
        notes.append(f"environment.python not checked (manifest {exp.get('environment', {}).get('python')}, "
                     f"this interpreter {platform.python_version()}; pass --check-python in the image)")
    got["environment"] = env

    for path, want, have in compare(exp, got):
        errors.append(f"{path}: manifest {json.dumps(want)[:80]} != recomputed {json.dumps(have)[:80]}")
    if data.get("commit") != SQLGLOT_COMMIT:
        errors.append(f"maps commit {data.get('commit')} != pinned {SQLGLOT_COMMIT}")
    fmt = compact_format(args.root)
    if fmt is not None and data.get("format") != fmt:
        errors.append(f"maps format {data.get('format')} != tools/compact_maps.py FORMAT {fmt}")
    c = got["counts"] or {}
    if (c.get("untrusted") or c.get("unmapped")) and not args.allow_untrusted:
        errors.append(f"{c.get('untrusted')} untrusted and {c.get('unmapped')} unmapped tests "
                      "(pass --allow-untrusted to accept)")

    drift = [k for k, v in generator_sources(args.root).items()
             if v and manifest.get("provenance", {}).get("generator_sha256", {}).get(k) not in (None, v)]
    if drift:
        notes.append("generator sources changed since the maps were built (provenance only): " + ", ".join(drift))
    return errors, notes, c


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--maps", default="maps/maps.bin")
    ap.add_argument("--tests", default="maps/tests.json")
    ap.add_argument("--manifest", default="maps/manifest.json")
    ap.add_argument("--repo", default=None, help="pinned sqlglot tree the maps were traced on")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="checkout holding Dockerfile, tools/Dockerfile.tools, requirements.lock")
    ap.add_argument("--allow-untrusted", action="store_true")
    ap.add_argument("--check-python", action="store_true", help="this interpreter must match the manifest")
    ap.add_argument("--python-version", default=None, help="with --write: record this instead of ours")
    ap.add_argument("--jobs", type=int, default=None,
                    help="with --write: the -j the maps were built with (type profile chunks)")
    ap.add_argument("--write", action="store_true", help="regenerate the manifest (needs --repo)")
    args = ap.parse_args(argv)
    try:
        if args.write:
            if not args.repo:
                raise Invalid("--write needs --repo (the pinned tree digest)")
            if args.jobs is None and os.path.exists(args.manifest):  # make manifest: keep the recorded -j
                try:
                    args.jobs = json.load(open(args.manifest)).get("provenance", {}).get("jobs")
                except ValueError:
                    pass
            m = build_manifest(args.maps, args.tests, args.repo, args.root,
                               args.python_version or platform.python_version(), args.jobs)
            problems = check_structure(load_maps(args.maps)[1], catalog(open(args.tests, "rb").read())[1])
            if problems:
                raise Invalid("refusing to write a manifest for these maps: " + "; ".join(problems))
            with open(args.manifest + ".tmp", "w") as fh:
                json.dump(m, fh, indent=1, sort_keys=True)
                fh.write("\n")
            os.replace(args.manifest + ".tmp", args.manifest)
            c = m["verified"]["counts"]
            print(f"[evidence] wrote {args.manifest}: maps {m['verified']['maps']['sha256'][:12]}, "
                  f"{c['ids']} ids, {c['untrusted']} untrusted", file=sys.stderr)
            return 0
        errors, notes, c = validate(args)
    except Invalid as error:
        print(f"[evidence] FAILED: {error}", file=sys.stderr)
        return 1
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
        print(f"[evidence] FAILED: malformed maps or manifest ({error!r})", file=sys.stderr)
        return 1
    for n in notes:
        print(f"[evidence] note: {n}", file=sys.stderr)
    for e in errors:
        print(f"[evidence] ERROR: {e}", file=sys.stderr)
    if errors:
        print(f"[evidence] FAILED: {len(errors)} problem(s)", file=sys.stderr)
        return 1
    print(f"[evidence] ok: {c.get('ids')} ids, {c.get('mapped')} mapped, {c.get('untrusted')} untrusted, "
          f"{c.get('typeprof_incomplete')} type-profile incomplete", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
