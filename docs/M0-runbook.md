# M0 runbook: fully traced maps, local sample, practice measurement

Goal (PRD §10 M0): commit fully traced `maps/maps.bin`, confirm the image scores ≈120.8 s
simulated with 0 misses on the sample, and record what the server reports for HEAD.

Nothing here pushes or submits. **Pushing and submitting are the owner's decisions.**
Run everything from the checkout you intend to commit from (not an agent worktree).
Durations on this Mac (Apple Silicon, `linux/amd64` emulated) are estimates; none were measured.

## a. Start Docker and check it

```sh
open -a Docker                 # wait until the whale icon says "Docker Desktop is running"
docker info --format 'cpus={{.NCPU}} mem={{.MemTotal}} cgroup=v{{.CgroupVersion}} os={{.OperatingSystem}}'
docker run --rm --platform linux/amd64 python:3.13-slim python -c 'import platform; print(platform.machine())'
```

Expect `cgroup=v2`, at least 4 CPUs and 8 GB (Settings > Resources), and `x86_64`.
Turn on Settings > General > "Use Rosetta for x86_64/amd64 emulation": under QEMU everything
below is several times slower and the time limits in section "Pitfalls" start to bite.
Failure: `failed to connect to the docker API ... docker.sock` means the daemon is not up yet.

`make` uses `JOBS` = the Docker VM's CPU count (`docker info`), else `nproc`/`sysctl hw.ncpu`.
Override with `make maps JOBS=6` if the VM runs out of memory (one traced pytest per job).

## b. Fully traced maps

```sh
python3 tools/map_stats.py maps/maps.bin          # before: ids 1620, mapped 1620, untrusted 37
ls work/maps/raw 2>/dev/null | head -1             # must be empty/missing, see note
time make maps 2>&1 | tee work/maps.log           # estimate 20-60 min emulated (README: 5-10 min native)
python3 tools/map_stats.py maps/maps.bin --check && echo FULLY-TRACED
```

Expected end of the log: `... 1620 mapped, 0 untrusted, ...` from `compact_maps.py`.
`map_stats.py` must print `untrusted 0`, `mapped 1620`, one `subtests` entry
(`test_generator_memory`) and two `exec` entries (`test_execute_tpch`, `test_execute_tpcds`);
an empty list there means that step's test did not pass and its replay is silently disabled.

**Resume trap:** `build_maps.py` skips every test that already has `work/maps/raw/<n>.s<seed>.json`,
including ones recorded as failed. A stale `work/maps/` (e.g. from a stub build) is reused as is.
For a clean build run `rm -rf work/maps` first; to retry only failures, delete those tests' raw
files (`grep -l '"outcome": "failed"' work/maps/raw/*.json`) and rerun `make maps`.

Failures:
* `make maps` **exits 0 even with untrusted tests.** Only `--check` (exit 1) or the
  `N untrusted` line tells you. The log shows each non-passing run as
  `[k/3240 ...] failed  tests/...`; its pytest tail is in the raw JSON (`"tail"`).
* A per-test timeout is 900 s (`test_max_nodes` is 28.8 s native, slower traced and emulated);
  a timed-out test is recorded as failed, i.e. untrusted.
* `build_typeprof.py` raises `TimeoutExpired` after 3600 s, or `exec_phases.py` exits with
  `cheap phase failed` / `phase ... failed`: `make` stops and `maps/maps.bin` is left unchanged
  (the new file is written as `maps/maps.bin.new` and renamed only at the end).

Commit (local only, on the branch the owner chooses):

```sh
shasum -a 256 maps/maps.bin
git add maps/maps.bin && git commit -m "Fully traced maps (make maps): 0 untrusted"
```

## c. Sample run and CPU

```sh
make sample-cpu 2>&1 | tee work/sample.log         # estimate 3-8 min incl. build; run itself < 3 min
make map-stats                                     # committed maps, then the maps inside the image
```

`sample-cpu` is `make sample` plus CPU: it runs the image with the scorer's flags, wrapped as
`sh -c 'python -I -S /app/run.py; cat /sys/fs/cgroup/cpu.stat >&2'`. With cgroup v2 the
container's `cpu.stat` `usage_usec` is user + system CPU of every process in the container,
which is what SCORING.md charges, and it is passed to `score.py --cpu-seconds`.
(`time docker run ...` only measures the client; `docker stats` samples and misses short runs.)
Run it 3 times and take the median, like the board.

Read the result:

```
[br] maps: 1620 tests, 0 untrusted, 0 unmapped     # 37 untrusted here = build repair failed
[br] done: 30 commits in Xs wall, Ys cpu           # Y = selector process only
usage_usec ...                                     # whole container
Simulated: 120.8 s      Misses: 0 (...)      CPU: ... s      Total: ... s
```

* Expected: Simulated ≈120.8 s, 0 misses (README, trusted maps). 565.8 s means 37 tests are
  still untrusted. Any miss is a stop: do not submit.
* Emulated CPU overstates the server's (Rosetta translation); treat it as an upper bound.
  The PRD's ≈4.4 s figure was local Mac CPU.
* If Simulated is above 120.8 with trusted maps, grep `work/sample.stderr` for
  `replica did not pass; kept` / `executor fingerprints changed; kept`: under emulation the
  replicas can exceed their 8 s / 12 s timeouts or the 80 s replay budget, which keeps tests
  that the server would drop. Not a selector bug by itself.
* `Rejected: ...` or a non-zero exit from `docker run` is a stop.

**Build repair (only while committed maps still have untrusted tests).** `make build` now uses
`--progress=plain`; in the `RUN python /app/tools/finish_maps.py` step look for:

| Line | Meaning |
| --- | --- |
| `[finish] 0 untrusted tests` | maps fully traced; step is a no-op (no pip install) |
| `[finish] 37 untrusted tests` ... `37 tests traced, 1620 mapped, 0 untrusted` ... `[finish] merged in Ns` | repair succeeded |
| `budget reached; merging what finished` | partial: count in the `compact_maps` line |
| `dependencies missing`, `... timed out; keeping maps`, `... failed; keeping maps`, `skipped after error` | no repair: image keeps 37 untrusted |

A `CACHED` step prints nothing: `make map-stats` (second half) is the authoritative count, or
rebuild with `docker build --no-cache --progress=plain --platform linux/amd64 -t blast-radius-entry .`.
The repair's budget is 200 s for pip install + retrace, plus up to 30+30+60 s for the index
steps; emulated on this Mac it may run out and leave some tests untrusted even though the
native server build repairs all of them (PRD §1). The repair also keeps the stub-built type
profile, subtest windows and executor phases from the base maps; only `make maps` rebuilds them.

`make truth-sample` (optional, estimate 30-90 min emulated): must end `30 commits agree, 0 differ`.
It exits 0 even when commits differ. Per-test timeout is 120 s, so under emulation
`ours only [1490]` (`test_max_nodes`) or `[1106]` (`test_execute_tpcds`) on many commits means a
timeout, not a label disagreement.

## d. Practice submission record (owner submits)

Before submitting, record:

```sh
git rev-parse HEAD HEAD^{tree}                                   # source digests
shasum -a 256 maps/maps.bin                                      # committed map digest
docker image inspect --format '{{.Id}}' blast-radius-entry       # local image id
docker run --rm --platform linux/amd64 blast-radius-entry \
  python /app/tools/map_stats.py /app/maps/maps.bin              # in-image map sha + untrusted
```

From the board, for **each of the 3 runs**: simulated, CPU, misses, total; plus the median total
and the time of submission. Valid only if all three runs are valid with 0 misses.

Re-derive the gate (PRD §8), with S = server practice simulated (median run), C = its CPU,
and 40 practice commits (inferred, not confirmed):

```
r               = S / 120.8                  # practice batch vs sample, same image
practice floor  F ≈ 94.351 * r               # sample zero-miss floor scaled
excess/commit   = (S + C - F) / 40           # compare with ≈1.03 s/commit expected for HEAD
release gate    = F + 0.2 * 40 = F + 8.0 s   # practice total; sample equivalent stays 100.4 s
2x target 119 s : feasible only if F < ≈115 s; sample-equivalent target = 119 / r
```

Write the measured r, F and gate back into PRD §8 in place of the estimates.

## Pitfalls checked statically

* Fixed: `JOBS ?= $(shell nproc)` gave an empty `-j` on a Mac without coreutils
  (`build_maps.py: argument -j: expected one argument`); and with Homebrew `nproc` it gave the
  host's 18 CPUs, not the VM's. Now the VM count first. `docker info` prints `0` while the
  daemon is down; that value is ignored.
* Fixed: `make maps` wrote `maps/maps.bin` in place; now via `maps/maps.bin.new` + rename.
* Fixed: BuildKit's default progress hid the repair output; `make build` uses `--progress=plain`.
* Documented: resume of stale `work/maps/raw`; `make maps` and `make truth-sample` exit 0 on
  untrusted maps or disagreeing labels; emulation vs 900 s / 120 s / 200 s / 8 s / 12 s limits.
* Documented: there is no `.dockerignore`, so `work/` (venvs, sqlglot copies, ~260 MB) is sent
  as build context on every build. Slow, not incorrect: the Dockerfile copies only
  `requirements.lock`, `tools`, `maps`, `selector`, `run.py`, so `sample/answers.json` never
  enters the image.
