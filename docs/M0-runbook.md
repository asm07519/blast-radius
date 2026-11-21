# M0 runbook: fully traced maps, local sample, practice measurement

Goal (PRD §10 M0): commit fully traced `maps/maps.bin` + `maps/manifest.json`, confirm the
image scores ≈118.6 s simulated with 0 misses on the sample, record what the server reports.

Nothing here pushes or submits. **Pushing and submitting are the owner's decisions.**

**Verified 2026-11-21** on Apple Silicon, Docker Desktop (18 CPUs, 7.7 GiB VM, Rosetta, cgroup v2),
everything `linux/amd64` emulated, with ~14 host cores busy with other jobs. Every duration and
CPU number below is from that setup: inflated, and only an upper bound for the server.
Use distinct `IMAGE=`/`TOOLS=` tags when other jobs share the Docker daemon.

## a. Docker

```sh
docker info --format 'cpus={{.NCPU}} mem={{.MemTotal}} cgroup=v{{.CgroupVersion}}'
```

Expect `cgroup=v2`, ≥4 CPUs, ~8 GB. Use Rosetta (Settings > General). Memory, not CPU, is the
limit: one traced pytest with duckdb/pandas peaks at ~0.8 GB (test_max_nodes ~2-3 GB), and
duckdb starts a thread per VM CPU in each process.

## b. Fully traced maps (`make maps`)

```sh
rm -rf work/maps                                   # resume trap: stale raw traces are reused
time make maps JOBS=6 2>&1 | tee work/maps.log     # JOBS=8 peaked at 7.2 GiB; 18 is OOM
python3 tools/map_stats.py maps/maps.bin --check && echo FULLY-TRACED
make validate SRC=/path/to/pinned/sqlglot          # host-side, no Docker
```

Measured, clean second build with JOBS=6: 21:38 total — trace 1162 s (3240 runs, 0.37 s/run;
test_max_nodes ≈ 50-130 s per seed), static 3 s, classes 1 s, typeprof 75 s, subtests 1 s,
exec_phases 22 s, compact 31 s, manifest 0 s. (First build, busier host: trace ≈ 29 min,
typeprof 122 s, exec_phases 37 s, compact 61 s.)

Determinism (two independent builds): every per-test call/import/reads/listed/classes record is
identical; maps differ only in `call_at_import` (+ `names`, which embeds it). Those entries come
from multiprocessing tests (executor, a few optimizer tests): the tracer's "an import is running"
flag is per process, not per thread, so a pool thread running sqlglot code while the main thread
imports is recorded at random. The extra entries only make class-literal narrowing more
conservative. `nproc` (trace shards per test) also varies for the executor tests.
`make validate`'s section checksums show exactly this.
Expected end of the log:

```
1620 tests traced, 1620 mapped, 0 untrusted, 245 files, 75213 call lines, 24824 import lines, ...
[evidence] wrote maps/manifest.json.new: maps 8279999f6f8d, 1620 ids, 0 untrusted
untrusted 0 / typeprof 0 incomplete / subtests [test_generator_memory] / exec [tpcds, tpch]
```

`make maps` exits 0 even with untrusted tests: only `--check` / `make validate` (exit 1) tell you.
Non-passing runs show as `[k/N ...] missing|failed|unclean-exit tests/...`; `rc -9` in the raw
JSON is the OOM killer. Such runs are retried once alone (`[retry alone] ...`); if one still
fails, delete its `work/maps/raw/<n>.s<seed>.json` and rerun `make maps` (only it is retraced,
the other steps rerun, ~5 min). The generator does not mount `sample/`.

Commit maps/maps.bin and maps/manifest.json together (local only; the owner picks the branch).

## c. Image build, sample run, CPU

```sh
make build                    # with complete maps: ~7 s here (base image cached)
make sample-cpu               # 3 times; the board takes the middle total
make map-stats                # committed maps, then the maps inside the image
```

Build log, `--progress=plain` (a `CACHED` step prints nothing; `--no-cache` to see it again):

```
[evidence] note: environment.base_image not checked (file not under --root)
[evidence] ok: 1620 ids, 1620 mapped, 0 untrusted, 0 type-profile incomplete
[finish] 0 untrusted tests
```

Any `[evidence] ERROR` / `FAILED` fails the build: the committed maps do not match
`maps/manifest.json` (stale, corrupted, or built for another catalog/tree/lock file); rerun
`make maps`. Image: 64.9 MB (content size), was 182 MB when the repair step had installed
`requirements.lock`.

`sample-cpu` runs the image with the scorer's flags, wrapped as
`sh -c 'python -I -S /app/run.py; cat /sys/fs/cgroup/cpu.stat >&2'`. Verified: as user 65534
under `--read-only --cap-drop ALL` the container sees its own cgroup (`/proc/self/cgroup` is
`0::/`), `cpu.stat` is world-readable, and `usage_usec` (user + system of every process in the
container) matched a 2 s busy loop. `SAMPLE_ENV="-e FLAG=1 ..."` passes env flags.

Measured (emulated, busy host):

| image / maps | flags | simulated | CPU s | total | misses | run wall |
| --- | --- | --- | --- | --- | --- | --- |
| committed maps before M0 (37 untrusted, repair failed), load ~25 | | 565.751 | 45.4 | 611.2 | 0 | 45 s |
| fully traced maps, 3 runs, load ~25 | | 118.628 | 7.27 / 7.36 / 7.28 | 125.904 middle | 0 | 6.7-6.9 s |
| same, `BR_LITERAL_GUARD=1 BR_LITERAL_PROBE=1`, load ~25 | flags | 118.628 | 9.70 / 9.70 / 9.53 | 128.333 middle | 0 | 9.1-9.3 s |
| fully traced maps, 3 runs, load ~10 | | 118.628 | 3.48 / 3.50 / 3.51 | **122.129** middle | 0 | 3.2 s |
| same, `BR_LITERAL_GUARD=1 BR_LITERAL_PROBE=1`, load ~10 | flags | 118.628 | 4.68 / 4.72 / 4.70 | 123.327 middle | 0 | 4.5 s |
| old maps repaired at build time by the fixed fallback, load ~10 | | 120.779 | 3.51 | 124.29 | 0 | 3.2 s |

CPU depends strongly on host load (2× between the two sessions above) and includes Rosetta
translation (system time ≈ 40% of a busy loop): treat it as an upper bound. The probe flags
cost 3 import probes (4d63, 535z, ktz3), ≈0.4-0.8 s each here. The selector's own `[br] done: ... 0.78s cpu` excludes its replica children.
Grep `work/sample.stderr` for `replica did not pass; kept` / `executor fingerprints changed; kept`
if simulated is above 118.6 s with trusted maps.

**Build-time repair** (`tools/finish_maps.py`, only while committed maps have untrusted tests):
with the old maps it did not work here: `37 untrusted tests` → `budget reached; merging what
finished` → `retraced 18 tests in 221s` → `static_index.py timed out; keeping maps` (the image kept
37 untrusted; build 5:48). Causes, now fixed: 18 parallel jobs OOM-killed the traced tests (rc -9,
counted untrusted), and the budget timeout left their pytest processes running. The server builds
natively on 4 cores, so it was likely unaffected (PRD §1). With the fix, the old maps repaired
fully here: `retracing with 7 jobs, 185s left` → `retraced 37 tests in 79s` → `37 tests traced,
1620 mapped, 0 untrusted` → `merged in 85s` (120.779 s simulated: the repair keeps the stub-built
type profile). With complete maps the step is a no-op.

`make truth-sample JOBS=6` (labeller in the tools image; exits 0 either way): verified
`30 commits agree, 0 differ` in 47 min with the default 120 s per-test timeout (peak 5.7 GiB).
Lost/timed-out chunks would show as `ours only [...]`; then raise the timeout with
`TRUTH_FLAGS="--test-timeout 600"`.

## d. Practice submission record (owner submits)

Before submitting, record:

```sh
git rev-parse HEAD HEAD^{tree}
shasum -a 256 maps/maps.bin && make validate
docker image inspect --format '{{.Id}}' blast-radius-entry
docker run --rm --platform linux/amd64 blast-radius-entry python /app/tools/map_stats.py /app/maps/maps.bin
```

From the board, for **each of the 3 runs**: simulated, CPU, misses, total; plus the median total
and the submission time. Valid only if all three runs are valid with 0 misses.

Re-derive the gate (PRD §8), with S = server practice simulated (median run), C = its CPU,
40 practice commits (inferred):

```
r               = S / 118.628                # practice batch vs sample, same image
practice floor  F ≈ 94.351 * r
excess/commit   = (S + C - F) / 40
release gate    = F + 0.2 * 40 = F + 8.0 s
```

Write the measured r, F and gate back into PRD §8 in place of the estimates.

## Pitfalls

* `make maps` / `make truth-sample` exit 0 on untrusted maps or disagreeing labels.
* Resume: `build_maps.py` skips every test with a raw file, including failed ones.
* `JOBS` defaults to the VM's CPU count (18 here): too many for 8 GB. Pass `JOBS=6`.
* `make sample-cpu` depends on `build`: editing anything under `tools/` reruns the repair step.
* Docker Desktop's file sharing on macOS left conflict copies when a container replaced a file
  the host had just deleted or renamed: `maps/maps 2.bin` (the old maps) and
  `work/maps/raw/1490.s1 2.json` (a killed run, which compact_maps.py would read as a record of
  test 1490 and mark it untrusted). After `make maps`, check `git status` and
  `ls work/maps/raw | grep -v '^[0-9]*\.s[0-9]*\.json$'`; delete such files.
* The labeller and the type profile run the suite in `JOBS` contiguous chunks: their results
  can depend on `JOBS` (state carried between tests inside a chunk).
