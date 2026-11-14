"""Build the sidecar as a PyInstaller onedir bundle (M0 plan item 7).

Run from anywhere with the project environment::

    uv run python apps/backend/packaging/build_sidecar.py

Output: build/sidecar/clipforge-sidecar/clipforge-sidecar.exe (gitignored).
PyInstaller is the version pinned in the dev dependency group. No shell is used.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

NAME = "clipforge-sidecar"
PACKAGING_DIR = Path(__file__).resolve().parent
BACKEND_DIR = PACKAGING_DIR.parent
REPO_ROOT = BACKEND_DIR.parents[1]
ENTRY = PACKAGING_DIR / "sidecar_entry.py"
MIGRATIONS_DIR = BACKEND_DIR / "clipforge" / "storage" / "migrations"
DIST_DIR = REPO_ROOT / "build" / "sidecar"
WORK_DIR = REPO_ROOT / "build" / "sidecar-work"
EXE = DIST_DIR / NAME / f"{NAME}.exe"
# Written next to (not inside) the bundle so the bundle folder holds only shipped files.
BUILD_INFO = DIST_DIR / "build-info.json"
# Imported only by name or from the migration files, which PyInstaller does not analyse.
HIDDEN_IMPORTS = (
    "alembic.context",  # migrations/env.py
    "alembic.op",  # revision scripts
    "alembic.ddl.sqlite",  # dialect implementation looked up by dialect name
    "sqlalchemy.dialects.sqlite.pysqlite",  # driver loaded by name from "sqlite+pysqlite"
)


def migration_data_args() -> list[str]:
    """``--add-data`` for every migration file, placed where the source tree has it.

    ``migrate.py`` resolves the scripts from its own ``__file__`` and Alembic loads them from
    files, so the migrations are data, not modules. ``__pycache__`` is not shipped.
    """
    args: list[str] = []
    for path in sorted(MIGRATIONS_DIR.rglob("*")):
        if not path.is_file() or "__pycache__" in path.relative_to(MIGRATIONS_DIR).parts:
            continue
        destination = path.parent.relative_to(BACKEND_DIR).as_posix()
        args += ["--add-data", f"{path}{os.pathsep}{destination}"]
    return args


def hidden_import_args() -> list[str]:
    args: list[str] = []
    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    return args


def main() -> int:
    if sys.platform != "win32":
        print("FAIL: the sidecar bundle is built on Windows only", file=sys.stderr)
        return 1

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--console",  # stdin/stdout are the launch contract (ADR 013)
        "--name",
        NAME,
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(WORK_DIR),
        "--specpath",
        str(WORK_DIR),
        "--paths",
        str(BACKEND_DIR),
        *migration_data_args(),
        *hidden_import_args(),
        "--log-level",
        "WARN",
        str(ENTRY),
    ]

    # A stale bundle must never pass for a fresh build.
    shutil.rmtree(DIST_DIR / NAME, ignore_errors=True)
    BUILD_INFO.unlink(missing_ok=True)

    started = time.monotonic()
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)  # noqa: S603
    elapsed = time.monotonic() - started
    if completed.returncode != 0:
        print(f"FAIL: PyInstaller exited with code {completed.returncode}", file=sys.stderr)
        return 1
    if not EXE.is_file():
        print(f"FAIL: expected executable was not produced: {EXE}", file=sys.stderr)
        return 1

    info = {"build_seconds": round(elapsed, 1), "pyinstaller": version("pyinstaller")}
    BUILD_INFO.write_text(json.dumps(info) + "\n", encoding="utf-8")
    print(f"BUILT {EXE}")
    print(f"build time: {elapsed:.1f} s (PyInstaller {info['pyinstaller']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
