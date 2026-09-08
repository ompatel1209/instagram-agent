"""Pytest wrapper: each standalone test file in its own subprocess.

The suite files stub requests/PIL in sys.modules before importing src and
were built to run one-per-process (`python3 tests/test_X.py`). pytest
collecting them in-process would collide stubs across files (src caches
whichever stub it imported first), so conftest.py excludes them from
collection and this file re-runs them isolated — `pytest tests/` becomes
8 green subprocess tests mirroring the native invocation, with a
per-file pass/fail.
"""
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
FILES = sorted(p for p in HERE.glob("test_*.py")
               if p.name != "test_standalone.py")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_standalone(path):
    r = subprocess.run([sys.executable, str(path)],
                       capture_output=True, text=True, timeout=240)
    assert r.returncode == 0, \
        f"{path.name} exited {r.returncode}\n{r.stdout}\n{r.stderr}"
