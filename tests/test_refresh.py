"""Tests for the daily trending refresh (src/refresh.py).

Runs with plain python3 — no pytest, no network, no requests: the requests
module is stubbed BEFORE src imports (music imports it at module level;
nothing in refresh's chain needs PIL, so no PIL stub). The GitHub queue
lookup is knob-patched (list / None / raise), and trending.CONTENT_DIR +
state.STATE_PATH point at a scratch dir so the committed content/hashtags.json
and state.json are never touched by a test run.

Covers the refresh contract:
  - deterministic per-date window (re-run stages the same tags), rotating
    daily, capped at TRENDING_NOW_COUNT, BLOCKED tags never surface;
  - content/hashtags.json structure preserved (_readme, every pool, key
    order, indent-2, trailing newline) with only trending_now replaced;
  - the staged window surfaces FIRST in trending.pick_trending for every
    vibe — the hook the day's captions actually read;
  - run() records st["refresh"] stats; a failed queue lookup (None return
    OR the RuntimeError _token() raises) records queue_remaining: null,
    never 0 — unknown must not masquerade as empty;
  - non-fatal: every step failure is noted and run() still returns 0 with
    st["refresh"] written.

Run:  python3 tests/test_refresh.py     (exit 0 = all passed)
"""
import datetime as dt
import functools
import json
import os
import shutil
import sys
import types
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ------------------------------------------------------------- stub requests
# src/music.py imports requests at module level (download_track); refresh's
# own chain never networks, so inert stubs suffice.
_requests = types.ModuleType("requests")


class _RequestException(Exception):
    pass


_requests.RequestException = _RequestException
_requests.get = lambda *a, **k: None
_requests.post = lambda *a, **k: None
_requests.head = lambda *a, **k: None
sys.modules["requests"] = _requests

# ---------------------------------------------------------- imports under test
from src import captions as captions_mod  # noqa: E402
from src import music, refresh, state, trending, uploads  # noqa: E402

# The run date is a PAST day (yesterday IST) — since the future-date guard
# landed (test_dates.py), overrides may be past/today only, never ahead.
# Computed once at import so every test in this file shares one stable date.
DATE = dt.datetime.now(refresh.IST).date() - dt.timedelta(days=1)
DATE_STR = DATE.isoformat()
NEXT_STR = dt.datetime.now(refresh.IST).date().isoformat()  # today: allowed

SCRATCH = ROOT / "tmp_refresh_test"
HASHTAGS = SCRATCH / "hashtags.json"
STATE_TMP = SCRATCH / "state.json"

# Knobs: what uploads.list_queue_checked returns (None = failed lookup),
# or raises (the _token RuntimeError that fires before its own try block).
FAKE = {"queue": None, "queue_exc": False}


def _fake_list_queue_checked():
    if FAKE["queue_exc"]:
        raise RuntimeError(
            "MEDIA_PUSH_TOKEN/GH_PAT not set — cannot list uploads")
    return FAKE["queue"]


uploads.list_queue_checked = _fake_list_queue_checked

# The seeded pool: 4 vibe pools + global, one deliberately BLOCKED tag
# ("likeforlike" in global) plus a stale trending_now that must be replaced.
SEED_POOL = {
    "_readme": "test pool — structure must survive the refresh",
    "trending_now": ["stalewindow"],
    "global": ["explorepage", "viral", "trending", "likeforlike"],
    "selfie": ["selfiequeen", "selfietime", "feelingmyself"],
    "attitude": ["attitudegirl", "sassyqueen", "thatgirl"],
    "cute": ["softaesthetic", "cozyvibes"],
    "general": ["goodvibesonly", "dailyinspo"],
}


def _seed_pool():
    """(Re)write the scratch hashtags.json to the pristine seed."""
    with open(HASHTAGS, "w", encoding="utf-8") as f:
        json.dump(SEED_POOL, f, indent=2, ensure_ascii=False)
        f.write("\n")


# Every test fixes the run date through the same env main.py honors.
os.environ["POST_DATE_OVERRIDE"] = DATE_STR


# ---------------------------------------------------------------------- runner
TESTS = []


def test(fn):
    @functools.wraps(fn)
    def wrapped():
        FAKE.update(queue=None, queue_exc=False)
        SCRATCH.mkdir(exist_ok=True)
        _seed_pool()
        STATE_TMP.unlink(missing_ok=True)
        trending.CONTENT_DIR = SCRATCH
        state.STATE_PATH = STATE_TMP
        music._library_cache = None
        os.environ["POST_DATE_OVERRIDE"] = DATE_STR
        fn()
    TESTS.append(wrapped)
    return wrapped


def _failures(st):
    return st.get("days", {}).get(DATE_STR, {}).get("failures", [])


def _run():
    code = refresh.run()
    assert code == 0, f"refresh.run() must return 0, got {code}"
    return state.load()


# --------------------------------------------------------------------- _today()
@test
def today_reads_post_date_override():
    assert refresh._today() == DATE
    os.environ["POST_DATE_OVERRIDE"] = NEXT_STR
    try:
        assert refresh._today() == dt.date.fromisoformat(NEXT_STR)
    finally:
        os.environ["POST_DATE_OVERRIDE"] = DATE_STR


@test
def today_falls_back_to_ist_now():
    del os.environ["POST_DATE_OVERRIDE"]
    try:
        assert refresh._today() == dt.datetime.now(refresh.IST).date()
    finally:
        os.environ["POST_DATE_OVERRIDE"] = DATE_STR


# ---------------------------------------------------------- _stage_trending_now
@test
def stage_is_deterministic_for_same_date():
    w1 = refresh._stage_trending_now(DATE)
    _seed_pool()  # restore the pristine pool, then stage again
    w2 = refresh._stage_trending_now(DATE)
    assert w1 == w2 and len(w1) >= 3, (w1, w2)


@test
def stage_rotates_daily():
    w1 = refresh._stage_trending_now(DATE)
    _seed_pool()
    w2 = refresh._stage_trending_now(dt.date.fromisoformat(NEXT_STR))
    assert w1 != w2, "consecutive days must stage different windows"


@test
def stage_excludes_blocked_tags():
    window = refresh._stage_trending_now(DATE)
    for tag in window:
        assert tag not in trending.BLOCKED, tag
    assert "likeforlike" not in window  # seeded in global on purpose
    allowed = set()
    for k, v in SEED_POOL.items():
        if k not in ("_readme", "trending_now"):
            allowed |= set(trending._clean(v))
    assert set(window) <= allowed  # never invents tags


@test
def stage_preserves_pool_structure():
    window = refresh._stage_trending_now(DATE)
    after = json.loads(HASHTAGS.read_text(encoding="utf-8"))
    assert list(after.keys()) == list(SEED_POOL.keys())  # key order kept
    for k, v in SEED_POOL.items():
        if k != "trending_now":
            assert after[k] == v, f"pool {k} must be untouched"
    assert after["trending_now"] == window
    assert "stalewindow" not in after["trending_now"]  # stale window gone
    text = HASHTAGS.read_text(encoding="utf-8")
    assert text.endswith("}\n") and not text.endswith("\n\n")
    assert '\n  "_readme"' in text  # state.py's indent-2 style


@test
def stage_window_is_capped():
    window = refresh._stage_trending_now(DATE)
    assert 0 < len(window) <= refresh.TRENDING_NOW_COUNT


@test
def staged_window_surfaces_first_in_pick_trending():
    window = refresh._stage_trending_now(DATE)
    assert window, "seeded pools must produce a non-empty window"
    for vibe in ("selfie", "attitude", "cute", "general"):
        picked = trending.pick_trending(vibe, DATE)
        assert picked[:3] == window[:3], (vibe, picked, window)
        assert set(window[:3]) <= set(picked)


# ------------------------------------------------------------------- run()
@test
def run_records_refresh_stats():
    st = {"days": {}, "posted_files": ["cute1.jpg"],
          "token": {"expires": "2026-09-20", "days_left": 5}}
    state.save(st)
    FAKE["queue"] = ["cute1.jpg", "attitude3.mp4"]
    expected_track = music.pick_track(DATE)  # deterministic, same manifest
    st2 = _run()
    rec = st2["refresh"]
    assert rec["date"] == DATE_STR
    assert rec["trending_now"] == json.loads(
        HASHTAGS.read_text(encoding="utf-8"))["trending_now"]
    assert rec["queue_remaining"] == 1
    assert rec["next_file"] == "attitude3.mp4"
    assert rec["vibe"] == captions_mod.vibe_from_filename("attitude3.mp4")
    assert rec["is_video"] is True
    assert rec["music_track"] == {"id": expected_track["id"],
                                  "mood": expected_track["mood"]}
    assert rec["token_days_left"] == 5
    assert _failures(st2) == []  # everything succeeded — no notes


@test
def run_queue_none_records_null_not_zero():
    FAKE["queue"] = None
    st2 = _run()
    rec = st2["refresh"]
    assert rec["queue_remaining"] is None  # unknown, NOT "0 files left"
    assert rec["next_file"] is None and rec["vibe"] is None \
        and rec["is_video"] is None
    assert any(f["where"] == "refresh" and "lookup failed" in f["message"]
               for f in _failures(st2))


@test
def run_queue_raise_records_null_not_zero():
    # uploads._token() raises BEFORE list_queue_checked's try/except, so
    # the RuntimeError escapes the function — run() must catch it itself.
    FAKE["queue_exc"] = True
    st2 = _run()
    assert st2["refresh"]["queue_remaining"] is None
    assert any(f["where"] == "refresh" and "lookup failed" in f["message"]
               for f in _failures(st2))


@test
def run_empty_queue_records_zero():
    FAKE["queue"] = []
    st2 = _run()
    rec = st2["refresh"]
    assert rec["queue_remaining"] == 0  # a real empty queue IS 0
    assert rec["next_file"] is None
    assert not any("queue" in f["message"] for f in _failures(st2))


@test
def run_stage_failure_is_non_fatal():
    trending.CONTENT_DIR = SCRATCH / "no-such-dir"  # load_pool will raise
    st2 = _run()
    rec = st2["refresh"]
    assert rec["trending_now"] is None
    assert any("trending_now refresh failed" in f["message"]
               for f in _failures(st2))
    # A failed stage must leave the pool file byte-identical to the seed
    # (the stale window stays in place — never a truncated/broken file).
    assert json.loads(HASHTAGS.read_text(encoding="utf-8")) == SEED_POOL


@test
def run_music_failure_is_non_fatal():
    orig = music.pick_track

    def boom(_date):
        raise RuntimeError("manifest missing")

    music.pick_track = boom
    try:
        FAKE["queue"] = ["attitude3.mp4"]
        st2 = _run()
    finally:
        music.pick_track = orig
    rec = st2["refresh"]
    assert rec["music_track"] is None
    assert any("music plan failed" in f["message"] for f in _failures(st2))
    assert rec["next_file"] == "attitude3.mp4"  # other steps still ran


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
    # scratch cleanup
    if "POST_DATE_OVERRIDE" in os.environ:
        del os.environ["POST_DATE_OVERRIDE"]
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run_tests())
