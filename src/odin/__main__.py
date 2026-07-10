"""Module entry point so Odin runs as ``python -m odin``.

Odin ships a console-script entry point (``odin``), but a module entry point
lets it run straight from a source checkout with no install at all:

    PYTHONPATH=src python -m odin guide

This is what lets downstream tooling depend on the maintained source (a plain
checkout) rather than packaging or vendoring a copy. Runtime is stdlib only.
"""

from __future__ import annotations

import sys

from odin.cli import main

sys.exit(main())
