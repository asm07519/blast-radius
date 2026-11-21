import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from selector.diag import main  # noqa: E402  (diagnostic build: selector.main.main + payload)

main()
