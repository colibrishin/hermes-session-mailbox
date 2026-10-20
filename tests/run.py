"""Run the Hermes interface suite: python tests/run.py [test names]

The tests run in a child interpreter, so every Hermes thread, SQLite handle and environment
lease is released before this parent removes the temporary fixture tree. Exit status is
non-zero when a test fails, errors or is skipped (a skip would hide an unchecked interface),
when no test ran, or when the fixture tree cannot be removed.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

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


def child(argv):
    """Run the selected tests in this (child) interpreter; any skip or expected failure fails the run."""
    sys.path.insert(0, str(HERE))
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(argv) if argv else loader.discover(str(HERE), top_level_dir=str(HERE))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    problems = []
    if result.skipped:
        problems.append(f"{len(result.skipped)} skipped: " + "; ".join(f"{test.id()}: {why}" for test, why in result.skipped))
    if result.expectedFailures or result.unexpectedSuccesses:
        problems.append("expected failures or unexpected successes are not allowed")
    if result.testsRun == 0:
        problems.append("no tests ran")
    for problem in problems:
        print(f"RUN FAILED: {problem}", flush=True)
    return 0 if result.wasSuccessful() and not problems else 1


def main(argv):
    base = Path(tempfile.mkdtemp(prefix="smb-ci-")).resolve()
    env = dict(os.environ, SMB_CI_BASE=str(base), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--child", *argv]
    print(f"python {sys.version.split()[0]} | {sys.executable} | fixture {base}", flush=True)
    proc = subprocess.run(command, cwd=HERE, env=env)
    try:
        rmtree_force(base)
    except Exception as exc:
        print(f"FIXTURE CLEANUP FAILED: {exc!r} {getattr(exc, 'filename', '') or ''}", flush=True)
        return 1
    print(f"fixture removed: {base}", flush=True)
    return proc.returncode


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        sys.exit(child(sys.argv[2:]))
    sys.exit(main(sys.argv[1:]))
