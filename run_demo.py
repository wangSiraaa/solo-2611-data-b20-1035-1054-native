#!/usr/bin/env python3
"""Small entry point: ``python run_demo.py``.

Pass ``--crossword`` to run the bilingual crossword end-to-end demo instead of
the materials/753 delivery demo.
"""

import sys

from delivery_system.cli import main


def _run() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--crossword":
        return main(["xw-demo"] + argv[1:])
    return main(["demo"] + argv)


if __name__ == "__main__":
    raise SystemExit(_run())
