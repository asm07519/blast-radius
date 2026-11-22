import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Safety refinements that passed their gates are on in the shipped entry point; set one to 0
# to get the old path back. `python -m selector.main` (tools, tests) leaves them off.
for _flag in (
    "BR_LITERAL_GUARD",
    "BR_LITERAL_PROBE",
    "BR_CRASH_GUARD",
    "BR_STRICT_PATHS",
    "BR_MUTATION_GUARD",
    "BR_HANG_GUARD",
    "BR_TPC_FIXTURE",
    "BR_CELLS",
):
    os.environ.setdefault(_flag, "1")

from selector.main import main  # noqa: E402

main()
