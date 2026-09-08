"""Pytest front-end for the standalone test suite.

Every tests/test_*.py file is a standalone runner by design: it injects
stub requests/PIL modules into sys.modules BEFORE importing src (that is
the whole contract — see any test file's docstring), so two files can
never share one interpreter (src caches whichever stub imported first).
This conftest keeps `pytest tests/` working anyway: the real files are
excluded from in-process collection, and tests/test_standalone.py re-runs
each one in its own subprocess.

The suite's native invocation stays `python3 tests/test_X.py`
(exit 0 = all passed).
"""
import pathlib


def pytest_ignore_collect(collection_path, config):
    name = pathlib.Path(collection_path).name
    return name.startswith("test_") and name != "test_standalone.py"
