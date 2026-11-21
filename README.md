# Blast Radius: dependency-map test selection for sqlglot

For each patch, select exactly the tests whose recorded execution can reach the
changed code or data, and split them over workers to minimise
`busiest worker + 2 s × workers`. Anything not understood selects every test
(~37 s) instead of risking a 300 s miss.

## How it works

**Maps, built once from the pinned commit** (`maps/maps.bin`, ~0.6 MB):

* Every test runs **alone in a fresh traced interpreter**. Shared caches (`lru_cache`,
  built-once tries) can never hide a dependency of one test behind another.
* `sys.monitoring` records each executed `(file, line)` once, split into
  **call-time** (inside a function, lambda or generator) and **import-time** (module
  or class body) code. Spawned `multiprocessing` children are traced too (via
  `sitecustomize`), and so are forked children that exit through `os._exit`.
* An audit hook records repository **files opened as data** (fixtures, sources read
  as text) and **directories listed**. Imports are excluded.
* A receiver hook records **which classes each test used**: the full MRO of `self`
  in `__init__`/`__new__`/copy hooks, and of `cls` in classmethods. A second hook,
  active only while modules execute, records what ran during imports.
* A static index of the pinned sources records where every name is read, plus
  computed-name attribute reads such as `getattr(self, f"{key}_sql")`, each with
  the regex of names it can produce.
* A **type profile** records the operand types that reach every single-operator
  `is` / `is not` / `==` / `!=` comparison in sqlglot over the whole suite. An import
  hook rewrites each comparison to record `(type(a), type(b))` and return the same result.
* **Per-subtest coverage** of `test_generator_memory`, one window per dialect.

**Per patch, at run time** (stdlib only, ~10 ms per commit):

1. Parse the unified diff as bytes. Apply it in memory to the pinned source in the image.
2. For Python files, diff the trees. Statements are first matched by exact source
   text, so comments and blank lines between statements never matter. Changed
   statements are then compared by AST, which ignores comments and formatting
   inside them. Each change becomes a site:
   * **call-time** (inside a function or lambda body). Selects tests that ran *any*
     line of the changed statement (whole span, because of short-circuit `and`/`or`
     across lines). For insertions, and for statements that emit no line event
     (`pass`, `global`, bare constants), it uses the neighbouring statements instead.
     If the only difference is a swap `is`↔`==` or `is not`↔`!=`, the site is dropped
     when every profiled operand-type pair makes `==` agree with identity: `None`,
     objects with default equality, members of one enum, or a hand-checked `__eq__`
     that is `False` for `None`. Only the few tests whose profiling run stopped early
     are kept.
   * **import-time** (module/class body, decorators, defaults). Selects every test
     that imported the module. Exception: a class-body change that only swaps literal
     values (`WEEK_OFFSET = -2`, `"constraint": True`) cannot fail at import, so it
     selects only tests that used that class or a subclass and, when the attribute
     is never read at import time, ran code that reads that attribute name. This
     falls back to all importers whenever the value can feed import-time state.
   * **docstring**. Selects tests that read the file's text.
   * **does not parse / coding cookie changed / patch does not apply**. Selects every
     importer.
3. Fixtures and other data files select the tests that opened them. Added, deleted
   or renamed files also select tests that listed their directory.
4. `conftest.py`, `sitecustomize.py`, top-level modules and changed pytest settings
   (`pyproject.toml [tool.pytest]`, `setup.cfg [tool:pytest]`, `pytest.ini`,
   `tox.ini`) select everything. Docs and other files nobody reads select nothing.
5. Tests the maps do not trust (see below) are added to every non-trivial commit.
6. **Subtest replica.** Suppose `test_generator_memory` (6.7 s) is selected and every
   change is a sqlglot edit that reaches only a few of its per-dialect subtests:
   call-time lines inside those windows, or a module first loaded inside the loop
   (then that window and every later one). The selector reruns exactly those
   subtests on the patched code, in ~0.2–1 s, and drops the test if they pass.
7. Schedule: longest-first packing for k = 1..8 with local improvement. Pick the k
   with the lowest `max load + 2 s·k`, in whole milliseconds like the scorer.

Every commit is wrapped in try/except, so an error or a time budget overrun selects
every test for that commit and never rejects the run.

## Results on the 30 sample commits

| maps | simulated | misses | selector CPU |
| --- | --- | --- | --- |
| committed (built without duckdb/pytz: 37 tests untrusted and added to every non-trivial commit) | ~680 s | 0 | ~0.3 s |
| all tests trusted (what full maps give, via `make maps` or the build-time retrace) | 120.8 s | 0 | ~1 s |

The first submission (before the type profile, the tighter statement rule and the
replica) scored 237.7 s on the practice set with 0 misses; on this sample it was at 182.5 s.

For reference: the starter scores 1614 s, "run everything, best split" 1104 s, and the oracle 94.4 s.

Unseen patches: 55 generated mutants were checked with `tools/ground_truth.py --complement-of`, which runs
every test the selector left out. They covered one-token code flips, class-attribute literals, line
deletions, comments (including inside strings), docstrings, syntax errors, fixture edits, test edits and
multi-file patches. None missed a failing test.

## Workflow

```sh
make sample        # build the submission image, run it like the scorer, score it
make maps          # rebuild maps/maps.bin in the labelling environment (real duckdb), ~5-10 min
                   # then commit maps/maps.bin
make truth-sample  # our labeller on the sample; must agree with sample/answers.json
make mutants truth-mutants eval-mutants   # generate patches, label them, count misses
```

`maps/maps.bin` in this repository was built in an environment without `duckdb` and
`pytz`. The tests that needed them are marked untrusted. During `docker build`,
`tools/finish_maps.py` installs `requirements.lock`, retraces only those tests and
merges them in (bounded to 4 minutes, never fails the build). Running `make maps`
once and committing the result makes that step a no-op and the build takes seconds.

## Layout

```
selector/          run-time code (stdlib only)
  main.py          entry point: read /data, select, schedule, print
  patch.py         unified diff parser/applier (bytes, new/deleted/renamed/binary)
  pyast.py         old/new source -> change sites
  impact.py        sites and paths -> tests; class-literal narrowing
  maps.py          map loader and bitset queries
  schedule.py      worker split
tools/             offline tooling (never run at scoring time)
  trace/           tracer, sitecustomize hook, pytest plugin
  build_maps.py    run each test alone under the tracer
  static_index.py  name reads and computed attribute reads in the pinned sources
  class_info.py    MRO and trackability of every sqlglot class
  compact_maps.py  fold traces (+ static data) into maps/maps.bin; merge mode
  finish_maps.py   image-build step: retrace untrusted tests when packages exist
  ground_truth.py  label patches like the organisers (full suite or complement only)
  mutants.py       generate patches of many kinds for miss hunting
  evaluate.py      run the selector on patches and score against labels
  stubs/           stand-ins for duckdb/pytz, used only where they are missing
```

## Rules

Maps hold only facts recorded from the pinned commit: coverage, imports, file reads,
class usage and static name reads. Nothing maps a patch or a kind of edit to the tests
it breaks. Generated mutants are used only to look for misses and are not shipped.
The build downloads only sqlglot at the pinned commit and PyPI packages from
`requirements.lock`.
