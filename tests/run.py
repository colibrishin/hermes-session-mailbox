"""Run the Hermes interface suite: python tests/run.py [test names]

The tests run in a child interpreter, so every Hermes thread, SQLite handle and environment
lease is released before this parent removes the temporary fixture tree. Exit status is
non-zero when a test fails or the fixture tree cannot be removed.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent


def rmtree_force(path):
    def clear_and_retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=clear_and_retry)
    else:
        shutil.rmtree(path, onerror=clear_and_retry)
    if Path(path).exists():
        raise AssertionError(f"fixture tree was not removed: {path}")


def main(argv):
    base = Path(tempfile.mkdtemp(prefix="smb-ci-")).resolve()
    env = dict(os.environ, SMB_CI_BASE=str(base), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    selection = ["-v", *argv] if argv else ["discover", "-v", "-s", str(HERE), "-t", str(HERE)]
    command = [sys.executable, "-B", "-m", "unittest", *selection]
    print(f"python {sys.version.split()[0]} | {sys.executable} | fixture {base}", flush=True)
    child = subprocess.run(command, cwd=HERE, env=env)
    try:
        rmtree_force(base)
    except Exception as exc:
        print(f"FIXTURE CLEANUP FAILED: {exc!r} {getattr(exc, 'filename', '') or ''}", flush=True)
        return 1
    print(f"fixture removed: {base}", flush=True)
    return child.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
