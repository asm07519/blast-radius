# v2 validation corpus (PRD §10)

Two sets of generated evaluation patches against the pinned sqlglot (`eb11aec4`):

| set | patches | unlabellable | set sha256 |
| --- | --- | --- | --- |
| development (`dev.manifest.json`, ids `d001`…) | 335 | 7 | `2744d1110185c707d7be44e5a005dbb3827a1868a3f29bea17d4b1ebe2f8b716` |
| frozen holdout (`holdout.manifest.json`, ids `h001`…) | 373 | 5 | `d41b101f8baa861ef5adca3ae33dca0c2021247b78f4f17da479a67108512b5c` |

Only the manifests are committed. Each one lists the generator version, seed, source digest, split rule, and,
per patch, its family/detail, the files and old-file lines it touches, its region keys, whether `git apply`
accepts it (`labellable`), and the sha256 of its bytes. Manifests hold no labels or test outcomes. The
patches and their labels stay in `work/`, which is gitignored. `validation/` is not copied into the
Docker image: the Dockerfile copies only `requirements.lock`, `tools`, `maps`, `selector` and `run.py`.
Nothing in `selector/` or the map builders imports `tools/mutants.py`.

**Holdout rule.** Do not tune anything on holdout results. Look at holdout labels only to gate a release (M1/M5),
and record every look.

## The split

* **Region.** A region is `path`, or `path#k` with `k = (line - 1) // 500` for files of 1500 lines or more,
  for everything under `sqlglot/expressions/`, and for `tests/helpers.py` (40-line buckets). A new file's
  region is its own path. A region belongs to `dev` iff `int(sha256("blast-radius-v2-split-1|" + region)[:8], 16)`
  is even. Every region a patch touches is in its set. So both sets cover `parser.py`, `generator.py`,
  `dialects/dialect.py` and `expressions/*`, but never the same bucket. Config files are single regions. As a
  result, pytest-settings edits (`pyproject.toml`, `setup.cfg`, `pytest.ini`, `tox.ini`), the root
  `conftest.py` and the root `sitecustomize.py` occur only in the holdout. Dev's `hook_config` patches are the
  `tests/` and `tests/dialects/` conftests and `tests/sitecustomize.py`.
* **Holdout-only families.** These stress Mode 0 and import-time narrowing. They never appear in dev:
  * `literal_init_error`: edits that look like literal swaps but raise while a module or class body runs:
    `-'x'`, `{[]: 1}`, `{(1, []): 2}`, `{[]}`, `-(1,)`, list keys and list set members.
  * `shared_state`: state that leaks between tests, so a test can pass alone but fail in suite order. Covers
    call counters that degrade after N calls, class/module tables mutated at call time, an `lru_cache` added
    to a builder that returns mutable trees, and the generator dispatch cache.
  * `compound_eq`: an `is`↔`==` swap plus an edit to a type/equality producer in another file (a linked
    callee's return, a `__eq__`/`__hash__`/`__bool__`, or an unrelated return). This can invalidate the
    type-profile shortcut.

Families in both sets: `m0_op` (135 per set, ≥35%: single-operator edits in dialect parser/generator code),
`insert_branch`, `class_table`, `import_edit`, `file_ops`, `hook_config`, `metadata`, `registry`,
`compound_mixed`, `runtime_error` (including one `os._exit` crash), `timeout` (3), `malformed`, `test_edit`,
and the pre-v2 kinds (`token_flip`, `class_literal`, `cmp_swap`, `comment`, `delete_line`, `syntax_error`,
`docs_file`, `hash_order`).

## Regenerate and verify the freeze

Use the pinned checkout. Python 3.13 built the manifests; 3.14 reproduces them too, with a warning:

```sh
PY=/Users/nick/Desktop/blast-radius/work/venv313/bin/python
SRC=/Users/nick/Desktop/blast-radius/work/sqlglot
V2=/Users/nick/Desktop/blast-radius/work/v2

# regenerate into work/ (byte-identical to the manifest) and re-check that git apply agrees
$PY tools/mutants.py --repo $SRC --split dev     --out $V2/dev     --clean --check-apply $V2/scratch
$PY tools/mutants.py --repo $SRC --split holdout --out $V2/holdout --clean --check-apply $V2/scratch

# verify the freeze: regenerate in memory, compare every patch's sha256, the set sha256 and the files on disk
$PY tools/mutants.py --repo $SRC --verify-manifest validation/holdout.manifest.json --out $V2/holdout
$PY tools/mutants.py --repo $SRC --verify-manifest validation/dev.manifest.json     --out $V2/dev
```

`--verify-manifest` exits non-zero on any difference: generator version, source digest, a patch's
bytes, the set hash, or extra or missing files. Changing the generator means a new `GEN_VERSION`
and a new holdout, not an edit to this one.

## Label

Labelling uses the full suite in catalog order (`tools/ground_truth.py`). Unlabellable patches (`git apply`
rejects them) are skipped and listed under `"unapplied"`. Check those separately: the selector's output for
them should be a safe superset. For the strict suite-order labels that the Mode 0 gate needs, use one pytest
process per patch:

```sh
$PY tools/ground_truth.py --repo $SRC --tests maps/tests.json --python $PY --tmp-dir $V2/tmp \
    --patches $V2/dev --out $V2/truth-dev.json --serial-order --patch-jobs 14 --suite-timeout 3600
$PY tools/ground_truth.py --repo $SRC --tests maps/tests.json --python $PY --tmp-dir $V2/tmp \
    --patches $V2/holdout --out $V2/truth-holdout.json --serial-order --patch-jobs 14 --suite-timeout 3600
```

A labelled patch costs about 80–100 CPU s. Faster but chunked labels (state does not carry across
chunks): `-j 2 --patch-jobs 7`. Runs resume from `<out>.d/`. In the output, `unknown` covers the baseline
failures plus tests without a complete report. `lost` lists tests that count as failing only because
their pytest process died or was killed.

## Evaluate

```sh
python3 tools/evaluate.py --patches $V2/dev --tests maps/tests.json --answers $V2/truth-dev.json --src $SRC
```

Join on the manifest's `family` for per-family results. `evaluate.py` reports every patch as kind
`generated`.
