IMAGE ?= blast-radius-entry
TOOLS ?= blast-radius-tools
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl
WORK := $(CURDIR)/work
JOBS ?= $(shell nproc)
MUTANTS ?= 150
SEED ?= 1

# The flags the scorer runs your image with.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

TOOLS_RUN := docker run --rm --platform linux/amd64 -v $(CURDIR)/maps:/app/maps -v $(WORK):/work \
	-v $(SAMPLE):/sample:ro $(TOOLS)

.PHONY: build sample check tools maps truth-sample mutants truth-mutants eval-mutants

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

# Build, run once on the sample under the scorer's limits, and score.
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

tools:
	docker build --platform linux/amd64 -f tools/Dockerfile.tools --tag $(TOOLS) .

# Rebuild maps/maps.bin from scratch in the labelling environment (real duckdb,
# pytz, ...): every test alone in a traced process, two hash seeds. Commit the result.
maps: tools
	mkdir -p $(WORK)
	$(TOOLS_RUN) sh -c "python tools/build_maps.py --repo /src/sqlglot --tests maps/tests.json \
		--work /work/maps -j $(JOBS) --seed 0 --seed 1 \
	 && python tools/static_index.py --repo /src/sqlglot --out /work/static.json \
	 && python tools/class_info.py --repo /src/sqlglot --out /work/classes.json \
	 && python tools/build_typeprof.py --repo /src/sqlglot --tests maps/tests.json --out /work/typeprof.json -j $(JOBS) \
	 && python tools/subtest_cov.py --repo /src/sqlglot \
		--test tests/test_generator.py::TestGenerator::test_generator_memory --out /work/subtests_genmem.json \
	 && python tools/compact_maps.py --tests maps/tests.json --raw /work/maps/raw --out maps/maps.bin \
		--static /work/static.json --classes /work/classes.json --typeprof /work/typeprof.json \
		--subtests /work/subtests_genmem.json"

# Label the sample with our own runner (must reproduce sample/answers.json).
truth-sample: tools
	mkdir -p $(WORK)
	$(TOOLS_RUN) python tools/ground_truth.py --repo /src/sqlglot --tests maps/tests.json \
		--patches /sample/commits --out /work/truth-sample.json -j $(JOBS)
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
