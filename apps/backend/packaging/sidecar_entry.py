"""PyInstaller entry point of the frozen sidecar.

Same launch contract as ``python -m clipforge.server`` (ADR 013); kept outside the
``clipforge`` package so the package carries no packaging-only code.
"""

from __future__ import annotations

import sys

from clipforge.server import main

if __name__ == "__main__":
    sys.exit(main())
