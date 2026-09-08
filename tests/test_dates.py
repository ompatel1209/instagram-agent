"""Tests for date resolution: future POST_DATE_OVERRIDE is refused.

Runs with plain python3 — no pytest, no network, no PIL, no requests: stub
modules are injected BEFORE src imports (the test_day_complete.py pattern),
so src.main (which pulls render->PIL and requests transitively) imports
clean.

The bug this guards against: three setup-time manual workflow_dispatch runs
each carried an explicit future POST_DATE_OVERRIDE (Sep 7/8/9 while it was
still Sep 4/5), published those days' content early, and marked the future
day keys complete in state.json — so the real Sep 8 and Sep 9 runs would
have exited "already published" and gone silent. The fix: resolve_date()
raises on any date past today IST. Past/today overrides remain allowed —
backfilling a missed day is the dispatch input's documented purpose, and
refresh._today() routes its override through the same guard.

Run:  python3 tests/test_dates.py     (exit 0 = all passed)
"""
import datetime as dt
import os
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ----------------------------------------------------------------- stub requests
_requests = types.ModuleType("requests")


class _RequestException(Exception):
    pass


_requests.RequestException = _RequestException
_requests.get = lambda *a, **k: None
_requests.post = lambda *a, **k: None
sys.modules["requests"] = _requests

# --------------------------------------------------------------------- stub PIL
_pil = types.ModuleType("PIL")
for _sub in ("Image", "ImageDraw", "ImageFont", "ImageOps"):
    _m = types.ModuleType(f"PIL.{_sub}")
    if _sub == "ImageFont":  # render.py type-annotates FreeTypeFont at def time
        _m.FreeTypeFont = object
    setattr(_pil, _sub, _m)
    sys.modules[f"PIL.{_sub}"] = _m
sys.modules["PIL"] = _pil

from src import main, refresh  # noqa: E402  (stubs must load first)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TODAY = dt.datetime.now(IST).date()

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ------------------------------------------------------------------ resolve_date

@test
def refuses_future_override():
    tomorrow = (TODAY + dt.timedelta(days=1)).isoformat()
    try:
        main.resolve_date(tomorrow)
    except ValueError as e:
        assert "future" in str(e), f"message should say future: {e}"
        assert tomorrow in str(e), f"message should name the date: {e}"
        return
    raise AssertionError("future override must raise ValueError")


@test
def refuses_far_future_override():
    # A whole month out — the guard must not depend on being "close".
    far = (TODAY + dt.timedelta(days=30)).isoformat()
    try:
        main.resolve_date(far)
    except ValueError:
        return
    raise AssertionError("far-future override must raise ValueError")


@test
def allows_past_override():
    yesterday = (TODAY - dt.timedelta(days=1)).isoformat()
    assert main.resolve_date(yesterday) == dt.date.fromisoformat(yesterday)


@test
def allows_today_override():
    assert main.resolve_date(TODAY.isoformat()) == TODAY


@test
def no_override_returns_today_ist():
    assert main.resolve_date(None) == TODAY


@test
def empty_string_override_returns_today():
    # post.yml passes github.event.inputs.date which is "" when untouched.
    assert main.resolve_date("") == TODAY


@test
def bad_format_still_raises_fromisoformat():
    # Non-ISO garbage must still fail loudly (fromisoformat ValueError).
    # ("20260908" is VALID basic-format ISO in py3.11+, so not in this list.)
    for bad in ("not-a-date", "2026-13-01", "2026-02-30"):
        try:
            main.resolve_date(bad)
        except ValueError:
            continue
        raise AssertionError(f"bad input {bad!r} must raise ValueError")


# ---------------------------------------------------------------- refresh mirror

@test
def refresh_today_no_override_is_today():
    os.environ.pop("POST_DATE_OVERRIDE", None)
    assert refresh._today() == TODAY


@test
def refresh_today_future_override_refused():
    tomorrow = (TODAY + dt.timedelta(days=1)).isoformat()
    old = os.environ.get("POST_DATE_OVERRIDE")
    os.environ["POST_DATE_OVERRIDE"] = tomorrow
    try:
        refresh._today()
    except ValueError:
        pass  # expected — and env must still be clean for later tests
    finally:
        if old is None:
            os.environ.pop("POST_DATE_OVERRIDE", None)
        else:
            os.environ["POST_DATE_OVERRIDE"] = old
    # Prove the guard fired (not that _today returned tomorrow silently):
    os.environ["POST_DATE_OVERRIDE"] = tomorrow
    raised = False
    try:
        refresh._today()
    except ValueError:
        raised = True
    finally:
        if old is None:
            os.environ.pop("POST_DATE_OVERRIDE", None)
        else:
            os.environ["POST_DATE_OVERRIDE"] = old
    assert raised, "refresh._today() must refuse a future override"


@test
def refresh_today_past_override_allowed():
    yesterday = (TODAY - dt.timedelta(days=1)).isoformat()
    old = os.environ.get("POST_DATE_OVERRIDE")
    os.environ["POST_DATE_OVERRIDE"] = yesterday
    try:
        assert refresh._today() == dt.date.fromisoformat(yesterday)
    finally:
        if old is None:
            os.environ.pop("POST_DATE_OVERRIDE", None)
        else:
            os.environ["POST_DATE_OVERRIDE"] = old


# ------------------------------------------------------------------- test runner

def _run_all() -> int:
    passed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001 — a crash is a failure
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"{passed}/{len(TESTS)} passed")
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(_run_all())
