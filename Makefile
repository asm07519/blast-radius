IMAGE ?= blast-radius-entry
TOOLS ?= blast-radius-tools
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl
WORK := $(CURDIR)/work
# CPUs of the Docker VM (on macOS that is not the host's count, and `nproc` may not exist).
# `docker info` prints 0 (and fails) while the daemon is down.
JOBS ?= $(shell n=$$(docker info --format '{{.NCPU}}' 2>/dev/null); [ "$$n" -gt 0 ] 2>/dev/null && echo $$n \
	|| nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)
MUTANTS ?= 150
SEED ?= 1
# Extra `docker run` flags for the sample targets, e.g. SAMPLE_ENV="-e BR_LITERAL_GUARD=1 -e BR_LITERAL_PROBE=1".
SAMPLE_ENV ?=

# The flags the scorer runs your image with.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

TOOLS_RUN := docker run --rm --platform linux/amd64 -v $(CURDIR)/maps:/app/maps -v $(WORK):/work \
	-v $(SAMPLE):/sample:ro $(TOOLS)

.PHONY: build sample sample-cpu map-stats check tools maps manifest validate truth-sample mutants truth-mutants eval-mutants

# Plain progress keeps the [finish] lines of the build-time map repair visible.
build:
	docker build --progress=plain --platform linux/amd64 --tag $(IMAGE) .

# Build, run once on the sample under the scorer's limits, and score.
sample: build
	docker run $(RUN_FLAGS) $(SAMPLE_ENV) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# Like `sample`, plus the CPU of every process in the container (user + system), read from
# its cgroup v2 cpu.stat as the container's last step. Selector log: work/sample.stderr.
sample-cpu: build
	mkdir -p $(WORK)
	docker run $(RUN_FLAGS) $(SAMPLE_ENV) $(IMAGE) sh -c 'python -I -S /app/run.py; rc=$$?; cat /sys/fs/cgroup/cpu.stat >&2; exit $$rc' \
		> $(OUTPUT) 2> $(WORK)/sample.stderr
	grep -E '^\[br\] (maps|done)|^usage_usec' $(WORK)/sample.stderr
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) \
		--cpu-seconds $$(awk '$$1 == "usage_usec" { printf "%.6f", $$2 / 1e6 }' $(WORK)/sample.stderr)

# Trust counts of the committed maps, then of the maps inside the built image.
map-stats:
	python3 tools/map_stats.py maps/maps.bin
	docker run --rm --platform linux/amd64 $(IMAGE) python /app/tools/map_stats.py /app/maps/maps.bin

check: build
	docker run $(RUN_FLAGS) $(SAMPLE_ENV) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

tools:
	docker build --platform linux/amd64 -f tools/Dockerfile.tools --tag $(TOOLS) .

# Rebuild maps/maps.bin from scratch in the labelling environment (real duckdb,
# pytz, ...): every test alone in a traced process, two hash seeds; then write
# maps/manifest.json. Commit both. The generator never sees sample/ (not mounted).
# One traced pytest per job: on a 8 GB Docker VM use JOBS<=8 (18 jobs ran out of memory).
# MAPDIR/MAPS_WORK send a second build to a scratch directory (determinism check).
MAPDIR ?= $(CURDIR)/maps
MAPS_WORK ?= /work/maps
MAPS_RUN = docker run --rm --platform linux/amd64 -e PYTHONHASHSEED=0 -v $(MAPDIR):/app/maps -v $(WORK):/work \
	-v $(CURDIR)/Dockerfile:/app/Dockerfile:ro -v $(CURDIR)/requirements.lock:/app/requirements.lock:ro $(TOOLS)
maps: tools
	mkdir -p $(WORK)
	$(MAPS_RUN) sh -ec 'step() { name=$$1; shift; t=$$(date +%s); echo "[maps] $$name" >&2; "$$@"; \
		echo "[maps] $$name done in $$(( $$(date +%s) - t ))s" >&2; }; W=$(MAPS_WORK); \
	step trace python tools/build_maps.py --repo /src/sqlglot --tests maps/tests.json \
		--work $$W -j $(JOBS) --seed 0 --seed 1; \
	step static python tools/static_index.py --repo /src/sqlglot --out $$W/static.json; \
	step classes python tools/class_info.py --repo /src/sqlglot --out $$W/classes.json; \
	step typeprof python tools/build_typeprof.py --repo /src/sqlglot --tests maps/tests.json \
		--out $$W/typeprof.json -j $(JOBS); \
	step subtests python tools/subtest_cov.py --repo /src/sqlglot \
		--test tests/test_generator.py::TestGenerator::test_generator_memory --out $$W/subtests_genmem.json; \
	step exec_phases python tools/exec_phases.py --repo /src/sqlglot --out $$W/exec_phases.json; \
	step compact python tools/compact_maps.py --tests maps/tests.json --raw $$W/raw --out maps/maps.bin.new \
		--static $$W/static.json --classes $$W/classes.json --typeprof $$W/typeprof.json \
		--subtests $$W/subtests_genmem.json --exec-phases $$W/exec_phases.json; \
	step manifest python tools/validate_evidence.py --maps maps/maps.bin.new --tests maps/tests.json \
		--manifest maps/manifest.json.new --repo /src/sqlglot --root /app --allow-untrusted --jobs $(JOBS) --write; \
	mv maps/maps.bin.new maps/maps.bin; mv maps/manifest.json.new maps/manifest.json'
	python3 tools/map_stats.py $(MAPDIR)/maps.bin

# Regenerate maps/manifest.json for the committed maps, in the labelling image: its
# Python and its pinned tree are what the manifest records.
manifest: tools
	$(MAPS_RUN) python tools/validate_evidence.py --maps maps/maps.bin --tests maps/tests.json \
		--manifest maps/manifest.json --repo /src/sqlglot --root /app --allow-untrusted --write

# Build sidecar maps/cells.bin (per-key table readers + traced dynamic reads) in the labelling
# image without touching maps/maps.bin. Use JOBS<=6 on an 8 GB Docker VM.
CELLS_WORK ?= /work/cells
cells: tools
	mkdir -p $(WORK)
	$(MAPS_RUN) sh -ec 'W=$(CELLS_WORK); \
	python tools/table_sites.py --repo /src/sqlglot --out $$W/table_sites.json; \
	python tools/build_cells.py --repo /src/sqlglot --tests maps/tests.json --work $$W -j $(JOBS) --seed 0 --seed 1; \
	python tools/compact_cells.py --tests maps/tests.json --maps maps/maps.bin --raw $$W/raw \
		--table-sites $$W/table_sites.json --out maps/cells.bin.new; \
	mv maps/cells.bin.new maps/cells.bin'

# Host-side, no Docker: recompute everything in maps/manifest.json (exit 1 on any mismatch
# or untrusted test). SRC, the pinned sqlglot tree, is optional: without it the tree
# digest is not checked.
SRC ?= $(WORK)/sqlglot
validate:
	python3 tools/validate_evidence.py --maps maps/maps.bin --tests maps/tests.json --manifest maps/manifest.json \
		$(if $(wildcard $(SRC)/sqlglot),--repo $(SRC))
	python3 -m unittest tests.test_validate_evidence

# Label the sample with our own runner (must reproduce sample/answers.json).
# Emulated on a Mac, a slow test can exceed the labeller's 120 s per-test timeout:
# TRUTH_FLAGS="--test-timeout 600".
TRUTH_FLAGS ?=
truth-sample: tools
	mkdir -p $(WORK)
	$(TOOLS_RUN) python tools/ground_truth.py --repo /src/sqlglot --tests maps/tests.json \
		--patches /sample/commits --out /work/truth-sample.json -j $(JOBS) $(TRUTH_FLAGS)
	python3 tools/compare_truth.py $(SAMPLE)/answers.json $(WORK)/truth-sample.json

SQLGLOT_COMMIT := eb11aec4be8cb2122afe713a4816662c3f0fb2fa

# A local copy of the pinned sqlglot, for the mutant generator and the evaluator.
$(WORK)/sqlglot:
	mkdir -p $@ && curl -sL https://github.com/tobymao/sqlglot/archive/$(SQLGLOT_COMMIT).tar.gz \
		| tar -xz -C $@ --strip-components=1

mutants: $(WORK)/sqlglot
	python3 tools/mutants.py --repo $(WORK)/sqlglot --out $(WORK)/mutants --count $(MUTANTS) --seed $(SEED)

truth-mutants: tools
	$(TOOLS_RUN) python tools/ground_truth.py --repo /src/sqlglot --tests maps/tests.json \
		--patches /work/mutants --out /work/truth-mutants.json -j $(JOBS)

eval-mutants: $(WORK)/sqlglot
	python3 tools/evaluate.py --patches $(WORK)/mutants --tests maps/tests.json \
		--answers $(WORK)/truth-mutants.json --src $(WORK)/sqlglot
