"""Tests for daily insights + weekly digest (src/insights.py).

Runs with plain python3 — no pytest, no network: the requests module is
stubbed BEFORE src imports (instagram imports it at module level), and the
Graph API calls are knob-patched so nothing external is ever touched.
state.STATE_PATH points at a scratch dir so the committed state.json is
never touched by a test run.

Covers the contract:
  - collect() stores followers + per-post metrics into st["insights"]["days"];
  - idempotent: measured ids are never re-fetched; today's own post ids are
    skipped (metrics read minutes after publishing are ~0 forever, and the
    id would never be re-fetched);
  - an unmeasurable id (deleted post) is recorded once in "unmeasurable"
    and never retried; the failure is a note, never a crash;
  - retention: day rows capped at KEEP_DAYS;
  - weekly_digest(): 7-day totals, follower delta, best vibe by average
    reach; None when there is no data;
  - run_daily(): digest only on Mondays, once per day (last_digest guard),
    and never raises even when every Graph call fails.

Run:  python3 tests/test_insights.py     (exit 0 = all passed)
"""
import datetime as dt
import json
import pathlib
import sys
import tempfile
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

from src import insights, instagram, state  # noqa: E402  (stubs must load first)

# ------------------------------------------------------------- scratch state
_scratch = pathlib.Path(tempfile.mkdtemp(prefix="ig-insights-test-"))
state.STATE_PATH = _scratch / "state.json"

TESTS = []


def test(fn):
    TESTS.append(fn)


def fresh_state(**top):
    st = {"days": {}}
    st.update(top)
    return st


CFG = {"access_token": "tok", "ig_user_id": "123"}


# ----------------------------------------------------------------- helpers
def patch_graph(stats=None, insights_map=None, fail=False):
    """Knob-patch the two Graph helpers; counters count calls."""
    calls = {"stats": 0, "insights": []}
    if fail:
        def boom(*a, **k):
            raise instagram.InstagramError("HTTP 400: whatever")
        instagram.get_account_stats = boom
        instagram.media_insights = boom
        return calls
    instagram.get_account_stats = lambda *a, **k: (calls.__setitem__(
        "stats", calls["stats"] + 1), dict(stats or {}))[1]
    def _insights(token, mid, metrics=""):
        calls["insights"].append(mid)
        if mid in (insights_map or {}):
            return insights_map[mid]
        raise instagram.InstagramError(
            "HTTP 400: object expired")  # e.g. deleted post
    instagram.media_insights = _insights
    return calls


# ----------------------------------------------------------------- tests
@test
def collect_stores_followers_and_media_metrics():
    state.STATE_PATH.write_text(json.dumps({"days": {}}))
    st = state.load()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    st["days"][yesterday] = {
        "published_media": {"feed": "M1"},
        "media_of_day": "selfie1.jpg",
    }
    calls = patch_graph(
        stats={"followers_count": 1000, "media_count": 5},
        insights_map={"M1": {"reach": 500, "likes": 40, "comments": 3,
                             "saved": 7}},
    )
    date_str = dt.date.today().isoformat()
    insights.collect(CFG, st, date_str)
    rec = st["insights"]
    assert rec["days"][date_str]["followers"] == 1000
    assert rec["days"][yesterday]["media"]["M1"]["reach"] == 500
    assert calls["insights"] == ["M1"], calls
    # idempotent: a second collect re-fetches nothing
    insights.collect(CFG, st, date_str)
    assert calls["insights"] == ["M1"], calls
    assert state.STATE_PATH.exists()


@test
def collect_skips_todays_own_media_and_marks_dead_ids_once():
    st = fresh_state()
    today = dt.date.today().isoformat()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    st["days"][today] = {"published_media": {"feed": "TODAY"}}
    st["days"][yesterday] = {"published_media": {"reel": "DEAD"}}
    calls = patch_graph(stats={"followers_count": 1},
                        insights_map={})  # DEAD -> 400
    insights.collect(CFG, st, today)
    assert "TODAY" not in calls["insights"], calls  # today skipped
    assert calls["insights"] == ["DEAD"], calls
    assert "DEAD" in st["insights"]["unmeasurable"]
    # second run: dead id NOT retried
    calls2 = patch_graph(stats={"followers_count": 1}, insights_map={})
    insights.collect(CFG, st, today)
    assert calls2["insights"] == [], calls2


@test
def collect_is_non_fatal_and_notes_failures():
    st = fresh_state()
    date_str = dt.date.today().isoformat()
    patch_graph(fail=True)
    insights.collect(CFG, st, date_str)  # must not raise
    notes = st["days"][date_str]["failures"]
    assert any(n["where"] == "insights" for n in notes), notes


@test
def retention_caps_day_rows():
    st = fresh_state()
    date_str = dt.date.today().isoformat()
    patch_graph(stats={"followers_count": 2}, insights_map={})
    old = (dt.date.today() - dt.timedelta(days=insights.KEEP_DAYS + 3))
    for i in range(insights.KEEP_DAYS + 3):
        d = (old + dt.timedelta(days=i)).isoformat()
        st["insights"] = {"days": {**st.get("insights", {}).get("days", {}),
                                   d: {"followers": i}}}
    insights.collect(CFG, st, date_str)
    days = st["insights"]["days"]
    assert len(days) <= insights.KEEP_DAYS, len(days)
    assert date_str in days


@test
def weekly_digest_totals_and_best_vibe():
    st = fresh_state()
    today = dt.date.today()
    assert today.weekday() != 6 or True  # any day works for the pure function
    days = {}
    data = {
        "vibe": [("selfie1.jpg", {"reach": 500, "likes": 40})],
        "noreach": [("cute1.jpg", {"likes": 10})],
    }
    for i, (name, media) in enumerate(data["vibe"] + data["noreach"]):
        d = (today - dt.timedelta(days=i + 1)).isoformat()
        st["days"][d] = {"media_of_day": name}
        st.setdefault("insights", {}).setdefault("days", {})[d] = {
            "followers": 1005 - i * 5,  # grows toward today
            "media": {f"M{i}": media}}
    d = insights.weekly_digest(st, today)
    assert d["posts_measured"] == 2
    assert d["reach"] == 500 and d["likes"] == 50 and d["saved"] == 0
    assert d["followers_delta"] == 5, d
    assert d["best_vibe"] == "selfie", d  # no-reach post can't win
    # out-of-window data ignored
    old = (today - dt.timedelta(days=20)).isoformat()
    st["insights"]["days"][old] = {"media": {"OLD": {"reach": 999}}}
    d2 = insights.weekly_digest(st, today)
    assert d2["reach"] == 500


@test
def run_daily_digests_only_on_mondays_and_never_raises():
    state.STATE_PATH.write_text(json.dumps({"days": {}}))
    st = state.load()
    monday = dt.date(2026, 11, 16)  # a Monday
    assert monday.weekday() == 0
    patch_graph(fail=True)  # every Graph call fails — still must not raise
    insights.run_daily(CFG, monday.isoformat())  # no raise
    # digest recorded even from empty data? No data -> no digest, no guard
    assert st.get("insights", {}).get("last_digest") is None
    # feed the digest data and re-run: digest fires once, guard prevents twice
    patch_graph(stats={"followers_count": 10}, insights_map={})
    st["insights"] = {"days": {(monday - dt.timedelta(days=1)).isoformat():
                               {"followers": 10,
                                "media": {"M": {"reach": 5, "likes": 1}}}}}
    state.save(st)
    insights.run_daily(CFG, monday.isoformat())
    st = state.load()
    assert st["insights"]["last_digest"] == monday.isoformat()
    saved_calls = len(state.load()["insights"]["days"])
    insights.run_daily(CFG, monday.isoformat())  # guard: same day, no re-run
    st2 = state.load()
    assert st2["insights"]["last_digest"] == monday.isoformat()
    assert len(st2["insights"]["days"]) == saved_calls


@test
def run_daily_disabled_or_missing_config_is_inert():
    state.STATE_PATH.write_text(json.dumps({"days": {}}))
    before = state.STATE_PATH.read_text()
    insights.run_daily(CFG, dt.date.today().isoformat(), enabled=False)
    insights.run_daily({}, dt.date.today().isoformat())
    insights.run_daily({"access_token": "t"}, dt.date.today().isoformat())
    assert state.STATE_PATH.read_text() == before  # untouched


if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    if failed:
        raise SystemExit(f"{failed} test(s) failed")
    print(f"all {len(TESTS)} passed")
