"""Tests for the Pexels-optional day-completion logic + queue URL encoding.

Runs with plain python3 — no pytest, no network, no PIL, no requests: stub
modules are injected BEFORE src imports (the test_engagement.py pattern), so
src.main (which pulls render->PIL and requests transitively) imports clean.

Covers the graceful-degradation contract: with a Pexels key a day is only
complete when feed+story+reel are all published (missing reel -> exit 1 ->
safety re-run retries); with no key the reel tier is disabled, so a photo-only
day must exit 0 on feed+story alone instead of retrying forever.

Run:  python3 tests/test_day_complete.py     (exit 0 = all passed)
"""
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

# ------------------------------------------------------------ imports under test
from src import main, uploads  # noqa: E402

CFG_KEY = {"pexels_api_key": "K"}
CFG_NO_KEY = {"pexels_api_key": ""}
DATE = "2026-09-09"

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def day(feed=True, story=True, reel=False):
    d = {}
    if feed:
        d["publish_feed"] = True
    if story:
        d["publish_story"] = True
    if reel:
        d["publish_reel"] = True
    return {"days": {DATE: d}}


# ------------------------------------------------- _day_complete_exit semantics
@test
def with_key_all_three_published_exits_zero():
    assert main._day_complete_exit(CFG_KEY, day(reel=True), DATE, "t") == 0


@test
def with_key_missing_reel_exits_one_for_retry():
    assert main._day_complete_exit(CFG_KEY, day(), DATE, "t") == 1


@test
def with_key_missing_feed_exits_one():
    st = day(feed=False, reel=True)
    assert main._day_complete_exit(CFG_KEY, st, DATE, "t") == 1


@test
def without_key_feed_story_only_exits_zero():
    # The graceful-degradation case: photo-only day, no stock reel possible.
    assert main._day_complete_exit(CFG_NO_KEY, day(), DATE, "t") == 0


@test
def without_key_missing_story_still_exits_one():
    st = day(story=False)
    assert main._day_complete_exit(CFG_NO_KEY, st, DATE, "t") == 1


@test
def without_key_video_day_with_reel_exits_zero():
    # Queue-video day: the reel slot is filled by the upload itself.
    assert main._day_complete_exit(CFG_NO_KEY, day(reel=True), DATE, "t") == 0


# ------------------------------------------------------- uploads.raw_url encoding
@test
def raw_url_percent_encodes_spaces():
    cfg = {"repo_owner": "o", "repo_name": "r"}
    assert uploads.raw_url("my video.mp4", cfg) == (
        "https://raw.githubusercontent.com/o/r/media/uploads/my%20video.mp4")


@test
def raw_url_leaves_safe_names_untouched():
    cfg = {"repo_owner": "o", "repo_name": "r"}
    assert uploads.raw_url("0selfie1.mp4", cfg).endswith(
        "/uploads/0selfie1.mp4")


# ---------------------------------------------------------------------- runner
def run_tests():
    failed = []
    for fn in TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as e:
            failed.append(fn.__name__)
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - len(failed)}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_tests())
