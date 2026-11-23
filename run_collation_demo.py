#!/usr/bin/env python3
"""Small entry point: ``python run_collation_demo.py``.

Publishes real packages through the delivery repository, then reproduces the
lead-type character-order investigation + binding scenarios.
"""

import sys

from delivery_system.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["collation-demo"] + sys.argv[1:]))
