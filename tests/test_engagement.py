"""Tests for the engagement feature (auto-replies) + new API-client methods.

Runs with plain python3 — no pytest, no network, no PIL. A stub `requests`
module is injected BEFORE src imports so src.instagram and src.alerts run
against a fake transport; the flows are exercised with patched API
functions (unittest.mock is stdlib, so nothing needs installing).

Run:  python3 tests/test_engagement.py     (exit 0 = all passed)
"""
import datetime as dt
import json
import pathlib
import sys
import tempfile
import types
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ----------------------------------------------------------------- stub requests
class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeRequests:
    """Records every call; pops queued responses (or raises them)."""

    def __init__(self):
        self.calls = []
        self.responses = []

    def _call(self, method, url, kw):
        self.calls.append((method, url, kw))
        if not self.responses:
            raise AssertionError(f"unexpected HTTP {method} {url} {kw}")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, **kw):
        return self._call("GET", url, kw)

    def post(self, url, **kw):
        return self._call("POST", url, kw)

    def request(self, method, url, **kw):
        return self._call(method, url, kw)


_fake = FakeRequests()
_mod = types.ModuleType("requests")
_mod.get = _fake.get
_mod.post = _fake.post
_mod.request = _fake.request
_mod.Response = FakeResponse
sys.modules["requests"] = _mod

# ------------------------------------------------------------ imports under test
from src import alerts, engagement, instagram, state  # noqa: E402
from src.engagement import MAX_COMMENT_REPLIES, MAX_DM_REPLIES  # noqa: E402

BANK = engagement.load_bank()
TODAY = "2026-09-04"
CFG = {"access_token": "TOK", "ig_user_id": "IG_USER",
       "handle": "@whoisaaniiiya"}

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ------------------------------------------------------------------- fixtures
def fresh_state():
    return {"days": {
        "2026-09-04": {"published_media": {"feed": "M_FEED",
                                           "story": "M_STORY"}},
        "2026-09-03": {"published_media": {"reel": "M_REEL"}},
    }}


def all_live(st):
    """Patch list_media so every recorded feed/reel id reads as still on the
    account — the reconciliation then changes nothing, which is the behavior
    the pre-reconciliation comment-flow tests were written against."""
    ids = engagement._recent_media_ids(st)
    return patch.object(
        instagram, "list_media",
        lambda t, u, limit=50: [{"id": i} for i in ids])


class TmpState:
    """Context: state.save/load go to a throwaway file, saves are counted."""

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        self._p1 = patch.object(
            state, "STATE_PATH",
            pathlib.Path(self._dir.name) / "state.json")
        self._p1.start()
        real_save = state.save
        self.saves = []

        def counting(st):
            self.saves.append(1)
            real_save(st)

        self._p2 = patch.object(state, "save", counting)
        self._p2.start()
        return self

    def __exit__(self, *exc):
        self._p2.stop()
        self._p1.stop()
        self._dir.cleanup()
        return False


def iso(hours_ago: int) -> str:
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S+0000")


def msg(mid, sender, text, when):
    return {"id": mid, "from": {"username": sender}, "text": text,
            "created_time": when}


def thread(tid, username, igsid, msgs, users=None):
    return {
        "id": tid,
        "users": users if users is not None else [
            {"username": "whoisaaniiiya", "id": "OWN_IGSID"},
            {"username": username, "id": igsid},
        ],
        "messages": {"data": msgs},
    }


def love_cat():
    return next(c for c in BANK["categories"] if c["key"] == "love")


def friendship_cat():
    return next(c for c in BANK["categories"] if c["key"] == "friendship")


# ----------------------------------------------------------------- the bank
@test
def bank_valid():
    cats = BANK["categories"]
    assert [c["key"] for c in cats] == ["love", "friendship", "general"]
    for c in cats:
        assert len(c["keywords"]) >= 10, c["key"]
        assert len(c["comment_replies"]) >= 8, c["key"]
        assert len(c["dm_replies"]) >= 6, c["key"]
        for kw in c["keywords"]:
            # a leading space would never substring-match real text
            assert isinstance(kw, str) and kw and not kw.startswith(" "), repr(kw)
        assert all(isinstance(r, str) and r for r in c["comment_replies"])
        assert all(isinstance(r, str) and r for r in c["dm_replies"])
    assert "whoisaaniiiya" in {
        str(u).lstrip("@").lower() for u in BANK["no_reply_users"]}


@test
def categorize_priority_and_fallback():
    # love is checked before friendship: both keywords present -> love
    assert engagement.categorize(BANK, "lets be friends love you")["key"] == "love"
    assert engagement.categorize(BANK, "wanna be friends")["key"] == "friendship"
    assert engagement.categorize(BANK, "nice pic")["key"] == "general"
    # no keyword matches -> the general category is the fallback
    assert engagement.categorize(BANK, "zzz qqq xyzzy")["key"] == "general"
    assert engagement.categorize(BANK, "")["key"] == "general"
    # substring + case-insensitive matching
    assert engagement.categorize(BANK, "SO BEAUTIFUL pic")["key"] == "love"


@test
def pick_replies_deterministic():
    love = love_cat()
    a = engagement._pick_replies(love, "comment_replies", "C100")
    b = engagement._pick_replies(love, "comment_replies", "C100")
    assert a == b                                # same seed -> same order
    assert sorted(a) == sorted(love["comment_replies"])  # a permutation
    firsts = {engagement._pick_replies(love, "comment_replies", f"C{i}")[0]
              for i in range(6)}
    assert len(firsts) >= 2                       # neighbors differ


@test
def own_usernames_from_bank_and_handle():
    own = engagement._own_usernames(BANK, CFG)
    assert "whoisaaniiiya" in own                 # bank no_reply_users
    assert "whoisaaniiiya" in engagement._own_usernames(
        BANK, {"handle": "whoisaaniiiya"})       # handle without @


# ------------------------------------------------------------------- state caps
@test
def state_lists_capped_at_50():
    st = {}
    for i in range(60):
        state.mark_comment_replied(st, f"C{i}")
    assert len(st["replied_comments"]) == state.ENGAGEMENT_CAP
    assert st["replied_comments"][0] == "C10"      # oldest trimmed
    assert st["replied_comments"][-1] == "C59"
    state.mark_comment_replied(st, "C59")          # re-mark is idempotent
    assert len(st["replied_comments"]) == state.ENGAGEMENT_CAP

    st2 = {}
    for i in range(60):
        state.mark_dm_replied(st2, f"T{i}")
    assert len(st2["dm_replied"]) == state.ENGAGEMENT_CAP

    st3 = {"days": {}}
    for i in range(60):
        state.mark_dm_thread_replied(st3, TODAY, f"T{i}")
    assert len(st3["days"][TODAY]["dm_replied_today"]) == state.ENGAGEMENT_CAP


@test
def dm_replied_today_roundtrip():
    st = {"days": {}}
    assert state.dm_replied_today(st, TODAY) == []
    state.mark_dm_thread_replied(st, TODAY, "T1")
    state.mark_dm_thread_replied(st, TODAY, "T1")
    assert state.dm_replied_today(st, TODAY) == ["T1"]


# ----------------------------------------------------------- API client methods
@test
def client_list_comments():
    _fake.responses = [FakeResponse(200, {"data": [{"id": "C1"}]})]
    out = instagram.list_comments("TOK", "M1")
    assert out == [{"id": "C1"}]
    method, url, kw = _fake.calls[-1]
    assert (method, url) == ("GET", f"{instagram.BASE}/M1/comments")
    p = kw["params"]
    assert p["access_token"] == "TOK" and p["limit"] == 30
    assert set("id text username timestamp".split()) <= set(p["fields"].split(","))
    assert "TOK" not in url                       # token rides in params, still not the URL path


@test
def client_reply_to_comment():
    _fake.responses = [FakeResponse(200, {"id": "R1"})]
    rid = instagram.reply_to_comment("TOK", "C9", "hii 🤍")
    assert rid == "R1"
    method, url, kw = _fake.calls[-1]
    assert (method, url) == ("POST", f"{instagram.BASE}/C9/replies")
    assert kw["data"] == {"message": "hii 🤍", "access_token": "TOK"}
    # missing id in the response -> explicit error
    _fake.responses = [FakeResponse(200, {"nope": 1})]
    try:
        instagram.reply_to_comment("TOK", "C9", "x")
        raise AssertionError("should have raised")
    except instagram.InstagramError:
        pass


@test
def client_list_conversations():
    _fake.responses = [FakeResponse(200, {"data": [{"id": "T1"}]})]
    out = instagram.list_conversations("TOK", "IG_USER")
    assert out == [{"id": "T1"}]
    method, url, kw = _fake.calls[-1]
    assert (method, url) == ("GET", f"{instagram.BASE}/IG_USER/conversations")
    p = kw["params"]
    assert p["platform"] == "instagram"
    assert "messages.limit(5)" in p["fields"]
    assert "users" in p["fields"] and "created_time" in p["fields"]


@test
def client_send_message_nested_payload():
    _fake.responses = [FakeResponse(200, {"message_id": "MID"})]
    mid = instagram.send_message("TOK", "IG_USER", "IG_123", "hii 🤍")
    assert mid == "MID"
    method, url, kw = _fake.calls[-1]
    assert (method, url) == ("POST", f"{instagram.BASE}/IG_USER/messages")
    assert "TOK" not in url                        # token in the JSON body, never the URL
    payload = kw["json"]
    assert payload["recipient"] == {"id": "IG_123"}
    assert payload["message"] == {"text": "hii 🤍"}
    assert payload["access_token"] == "TOK"
    assert "data" not in kw                        # JSON body, not form-encoded


@test
def client_error_shape():
    _fake.responses = [FakeResponse(
        403, {"error": {"message": "(#10) Application does not have permission"}})]
    try:
        instagram.list_comments("TOK", "M")
        raise AssertionError("should have raised")
    except instagram.InstagramError as e:
        assert "403" in str(e) and "permission" in str(e)


# ---------------------------------------------------------------- comment flow
@test
def comment_flow_replies_skips_and_saves():
    st = fresh_state()
    st["replied_comments"] = ["C_DONE"]
    comments = [
        {"id": "C_OWN", "text": "love this 💗", "username": "whoisaaniiiya"},
        {"id": "C_DONE", "text": "beautiful", "username": "fan1"},
        {"id": "C_LOVE", "text": "so beautiful 😍", "username": "fan2"},
        {"id": "C_FRAND", "text": "wanna be friends", "username": "fan3"},
    ]
    replied = []
    with TmpState() as ts:
        with all_live(st), \
             patch.object(instagram, "list_comments",
                          lambda t, m: comments), \
             patch.object(instagram, "reply_to_comment",
                          lambda t, c, m: replied.append((c, m)) or "RID"):
            n, perm = engagement.reply_to_comments(CFG, st, BANK, TODAY)
        assert n == 2 and perm is None
        assert [c for c, _ in replied] == ["C_LOVE", "C_FRAND"]
        assert "C_LOVE" in st["replied_comments"]
        assert "C_FRAND" in st["replied_comments"]
        assert len(st["replied_comments"]) == 3   # C_DONE + 2 new, no dupes
        assert len(ts.saves) == 2                  # state saved per reply
    assert replied[0][1] in love_cat()["comment_replies"]
    assert replied[1][1] in friendship_cat()["comment_replies"]


@test
def comment_cap_respected():
    st = fresh_state()
    comments = [{"id": f"C{i}", "text": "wow amazing", "username": "fan"}
                for i in range(30)]
    with TmpState() as ts:
        with all_live(st), \
             patch.object(instagram, "list_comments",
                          lambda t, m: comments), \
             patch.object(instagram, "reply_to_comment",
                          lambda t, c, m: "RID"):
            n, perm = engagement.reply_to_comments(CFG, st, BANK, TODAY)
        assert n == MAX_COMMENT_REPLIES and perm is None
        assert len(ts.saves) == MAX_COMMENT_REPLIES


@test
def comment_permission_problem_flagged():
    st = fresh_state()

    def raise_perm(t, m):
        raise instagram.InstagramError(
            "HTTP 403: (#10) Application does not have permission for this action")

    with TmpState():
        with all_live(st), patch.object(instagram, "list_comments", raise_perm):
            n, perm = engagement.reply_to_comments(CFG, st, BANK, TODAY)
        assert perm == "comments"
        failures = st["days"][TODAY]["failures"]
        assert any("permission" in f["message"] for f in failures)


@test
def comment_nonperm_error_returns_no_flag():
    st = fresh_state()

    def raise_boom(t, m):
        raise instagram.InstagramError("HTTP 500: transient boom")

    with TmpState():
        with all_live(st), patch.object(instagram, "list_comments", raise_boom):
            n, perm = engagement.reply_to_comments(CFG, st, BANK, TODAY)
        assert n == 0 and perm is None


@test
def story_comments_error_is_not_a_permission_problem():
    # The EXACT live error from the scope probe (run 33898808806): a
    # Story media id has no comments edge, and Graph's generic 400 names
    # "missing permissions" among its guesses. That must NOT be read as a
    # scope problem — and one bad media object must not abort the pass.
    st = fresh_state()
    seen = []

    def list_comments(t, mid):
        seen.append(mid)
        if mid == "M_STORY":            # no story in state? raise anyway below
            raise instagram.InstagramError(
                "HTTP 400: Unsupported get request. Object with ID "
                "'18618027775052203' does not exist, cannot be loaded due "
                "to missing permissions, or does not support this operation")
        return [{"id": "C_OK2", "text": "wow", "username": "fan9"}]

    with TmpState():
        with all_live(st), \
             patch.object(instagram, "list_comments", list_comments), \
             patch.object(instagram, "reply_to_comment",
                          lambda t, c, m: "RID"):
            n, perm = engagement.reply_to_comments(CFG, st, BANK, TODAY)
        assert perm is None                 # generic 400 ≠ scope problem
        assert n == 1                       # feed post still answered
        assert "M_STORY" not in seen        # stories not queried at all
        assert seen == ["M_FEED", "M_REEL"]  # pass continued past errors


@test
def comment_reply_failure_continues():
    st = fresh_state()
    comments = [
        {"id": "C_BAD", "text": "wow", "username": "fan1"},
        {"id": "C_OK", "text": "nice pic", "username": "fan2"},
    ]

    def reply(t, cid, m):
        if cid == "C_BAD":
            raise instagram.InstagramError("HTTP 500: boom")
        return "RID"

    with TmpState():
        with all_live(st), \
             patch.object(instagram, "list_comments",
                          lambda t, m: comments), \
             patch.object(instagram, "reply_to_comment", reply):
            n, perm = engagement.reply_to_comments(CFG, st, BANK, TODAY)
        assert n == 1 and perm is None
        assert "C_OK" in st["replied_comments"]
        assert "C_BAD" not in st["replied_comments"]
        assert any(f.get("where") == "engagement"
                   for f in st["days"][TODAY]["failures"])


@test
def all_media_denied_generic_is_a_permission_problem():
    # The second live probe (run 33899788977): EVERY feed/reel media object
    # the agent itself published 400s on the comments edge with Graph's
    # generic error. The objects exist and stories are never queried, so
    # by elimination the cause is the missing
    # instagram_business_manage_comments permission — that must be flagged
    # (it opens the alert issue that tells the user to extend the token).
    st = fresh_state()

    def denied(t, mid):
        raise instagram.InstagramError(
            "HTTP 400: Unsupported get request. Object with ID "
            f"'{mid}' does not exist, cannot be loaded due to missing "
            "permissions, or does not support this operation")

    with TmpState():
        with all_live(st), patch.object(instagram, "list_comments", denied):
            n, perm = engagement.reply_to_comments(CFG, st, BANK, TODAY)
        assert n == 0 and perm == "comments"

    # But a MIXED failure (one generic + one 500) must NOT be read as a
    # scope problem — that shape is an outage or a deleted post.
    st2 = fresh_state()
    calls = []

    def mixed(t, mid):
        calls.append(mid)
        if mid == "M_FEED":
            raise instagram.InstagramError(
                "HTTP 400: Unsupported get request. Object with ID "
                "'M_FEED' does not exist, cannot be loaded due to missing "
                "permissions, or does not support this operation")
        raise instagram.InstagramError("HTTP 500: transient")

    with TmpState():
        with all_live(st2), patch.object(instagram, "list_comments", mixed):
            n, perm = engagement.reply_to_comments(CFG, st2, BANK, TODAY)
        assert n == 0 and perm is None
        assert calls == ["M_FEED", "M_REEL"]   # pass continued past both


# ---------------------------------------------- live-media reconciliation
@test
def client_list_media():
    _fake.responses = [FakeResponse(200, {"data": [{"id": "M1"}, {"id": "M2"}]})]
    out = instagram.list_media("TOK", "IG_USER")
    assert out == [{"id": "M1"}, {"id": "M2"}]
    method, url, kw = _fake.calls[-1]
    assert (method, url) == ("GET", f"{instagram.BASE}/IG_USER/media")
    p = kw["params"]
    assert p["fields"] == "id" and p["limit"] == 50
    assert p["access_token"] == "TOK"
    assert "TOK" not in url                # token in params, never the URL
    # custom limit rides through
    _fake.responses = [FakeResponse(200, {"data": []})]
    assert instagram.list_media("TOK", "IG_USER", limit=7) == []
    assert _fake.calls[-1][2]["params"]["limit"] == 7


@test
def dead_media_ids_skipped_not_flagged():
    # Probe-3 finding: state holds ids the account no longer has. The
    # reconciliation must skip them BEFORE any comments call — no failure
    # notes (an hourly run would flood the 20/day cap), no all-denied
    # permission flag from media that merely isn't there anymore.
    st = fresh_state()

    def live(t, u, limit=50):
        return [{"id": "M_FEED"}]          # only the feed id is live

    comments = [{"id": "C1", "text": "so cute", "username": "fan"}]
    replied = []
    with TmpState():
        with patch.object(instagram, "list_media", live), \
             patch.object(instagram, "list_comments",
                          lambda t, m: comments), \
             patch.object(instagram, "reply_to_comment",
                          lambda t, c, m: replied.append(m) or "RID"):
            n, perm = engagement.reply_to_comments(CFG, st, BANK, TODAY)
        assert n == 1 and perm is None
        assert replied                      # the LIVE post still gets answered
        assert st["days"][TODAY].get("failures") is None   # zero noise
        # M_REEL (dead) never reached list_comments — 5 recent ids include
        # it, but only M_FEED was queried.


@test
def all_live_denied_still_flags():
    # The all-denied flag must count only LIVE media: token scope problems
    # deny exactly the live set, so live-everything-denied still flags
    # "comments" — the reconciliation must not be able to silence it.
    st = fresh_state()

    def live(t, u, limit=50):
        return [{"id": "M_FEED"}, {"id": "M_REEL"}]   # everything is live

    def denied(t, mid):
        raise instagram.InstagramError(
            "HTTP 400: Unsupported get request. Object with ID "
            f"'{mid}' does not exist, cannot be loaded due to missing "
            "permissions, or does not support this operation")

    with TmpState():
        with patch.object(instagram, "list_media", live), \
             patch.object(instagram, "list_comments", denied):
            n, perm = engagement.reply_to_comments(CFG, st, BANK, TODAY)
        assert n == 0 and perm == "comments"


@test
def dead_and_live_mix_does_not_flag():
    # Probe-3's exact live shape: 1 of the 5 recent ids live and working,
    # 4 dead (400 generic on the comments edge). The pre-reconciliation
    # code correctly refused to flag it — the reconciliation must keep
    # that verdict: it prunes the dead ids first, so the flag only looks
    # at the live set (which here is healthy).
    st = {"days": {
        "2026-09-04": {"published_media": {
            "feed": "M_LIVE", "reel": "M_DEAD1"}},
        "2026-09-03": {"published_media": {
            "feed": "M_DEAD2", "reel": "M_DEAD3"}},
    }}

    def live(t, u, limit=50):
        return [{"id": "M_LIVE"}]

    def list_comments(t, mid):
        assert mid == "M_LIVE", f"dead id {mid} queried"
        return []                          # live post, simply no comments

    with TmpState():
        with patch.object(instagram, "list_media", live), \
             patch.object(instagram, "list_comments", list_comments):
            n, perm = engagement.reply_to_comments(CFG, st, BANK, TODAY)
        assert n == 0 and perm is None


@test
def list_media_failure_falls_back_to_full_sweep():
    # The reconciliation itself must never be able to break the sweep:
    # when /media 500s we fall back to sweeping every recorded id exactly
    # as before (dead posts then surface as ordinary per-media notes).
    st = fresh_state()
    queried = []

    def boom(t, u, limit=50):
        raise instagram.InstagramError("HTTP 500: media lookup boom")

    def list_comments(t, mid):
        queried.append(mid)
        return []

    with TmpState():
        with patch.object(instagram, "list_media", boom), \
             patch.object(instagram, "list_comments", list_comments):
            n, perm = engagement.reply_to_comments(CFG, st, BANK, TODAY)
        assert n == 0 and perm is None
        assert queried == ["M_FEED", "M_REEL"]   # full unfiltered sweep
        assert any("live media lookup failed" in f["message"]
                   for f in st["days"][TODAY]["failures"])


@test
def empty_live_list_treated_as_unknown():
    # list_media succeeding with an empty payload (a glitch) must not be
    # read as "the account has nothing live": pruning on that would zero
    # the sweep and permanently silence the all-denied flag. Fall back
    # to the full sweep instead.
    st = fresh_state()
    queried = []

    def empty(t, u, limit=50):
        return []

    def list_comments(t, mid):
        queried.append(mid)
        return []

    with TmpState():
        with patch.object(instagram, "list_media", empty), \
             patch.object(instagram, "list_comments", list_comments):
            n, perm = engagement.reply_to_comments(CFG, st, BANK, TODAY)
        assert n == 0 and perm is None
        assert queried == ["M_FEED", "M_REEL"]   # nothing was pruned


# --------------------------------------------------------------------- DM flow
@test
def dm_flow_answers_only_fresh_incoming():
    st = fresh_state()
    st["days"][TODAY]["dm_replied_today"] = ["T_AGAIN"]
    threads = [
        # fresh + incoming + participant known -> answered
        thread("T_REPLY", "niceperson", "IG_1", [
            msg("a0", "whoisaaniiiya", "ty 🤍", iso(3)),      # our older reply
            msg("a1", "niceperson", "hii wanna be friends?", iso(2)),
        ]),
        # newest message is ours -> already answered, skip
        thread("T_OWN_LAST", "someone", "IG_2", [
            msg("b1", "someone", "hello", iso(5)),
            msg("b2", "whoisaaniiiya", "hey! 🤍", iso(4)),
        ]),
        # older than the 24h window -> skip
        thread("T_OLD", "oldie", "IG_3", [
            msg("c1", "oldie", "hey", iso(30)),
        ]),
        # unparseable timestamp -> can't prove freshness, skip
        thread("T_NODATE", "mystery", "IG_4", [
            msg("d1", "mystery", "sup", "not-a-time"),
        ]),
        # already answered today -> skip
        thread("T_AGAIN", "repeat", "IG_5", [
            msg("e1", "repeat", "hii", iso(1)),
        ]),
        # no participant IGSID (only us in users) -> skip with a note
        thread("T_NOIGSID", "whoisaaniiiya", None, [
            msg("f1", "stranger", "hello there", iso(1)),
        ], users=[{"username": "whoisaaniiiya", "id": "OWN_IGSID"}]),
        # empty message list -> skip
        thread("T_EMPTY", "quiet", "IG_6", []),
    ]
    sent = []
    with TmpState() as ts:
        with patch.object(instagram, "list_conversations",
                          lambda t, u: threads), \
             patch.object(instagram, "send_message",
                          lambda t, u, r, txt: sent.append((r, txt)) or "MID"):
            n, perm = engagement.answer_dms(CFG, st, BANK, TODAY)
        assert n == 1 and perm is None
        assert len(sent) == 1
        assert sent[0][0] == "IG_1"                # the participant IGSID
        assert sent[0][1] in friendship_cat()["dm_replies"]
        assert st["dm_replied"] == ["T_REPLY"]      # never-ever-again list
        assert st["days"][TODAY]["dm_replied_today"] == ["T_AGAIN", "T_REPLY"]
        assert len(ts.saves) == 1
        assert any("no participant IGSID" in f["message"]
                   for f in st["days"][TODAY]["failures"])


@test
def dm_cap_respected():
    st = fresh_state()
    threads = [thread(f"T{i}", f"u{i}", f"IG_{i}", [
        msg(f"m{i}", f"u{i}", "hii", iso(1))]) for i in range(8)]
    with TmpState() as ts:
        with patch.object(instagram, "list_conversations",
                          lambda t, u: threads), \
             patch.object(instagram, "send_message",
                          lambda t, u, r, txt: "MID"):
            n, perm = engagement.answer_dms(CFG, st, BANK, TODAY)
        assert n == MAX_DM_REPLIES and perm is None
        assert len(ts.saves) == MAX_DM_REPLIES


@test
def dm_permission_problem_flagged():
    st = fresh_state()

    def raise_perm(t, u):
        raise instagram.InstagramError(
            "HTTP 400: (#200) The user must authorize the "
            "instagram_business_manage_messages permission")

    with TmpState():
        with patch.object(instagram, "list_conversations", raise_perm):
            n, perm = engagement.answer_dms(CFG, st, BANK, TODAY)
        assert n == 0 and perm == "messages"


@test
def dm_send_failure_continues():
    st = fresh_state()
    threads = [
        thread("T_BAD", "u1", "IG_1", [msg("m1", "u1", "hii", iso(1))]),
        thread("T_OK", "u2", "IG_2", [msg("m2", "u2", "hii", iso(1))]),
    ]

    def send(t, u, r, txt):
        if r == "IG_1":
            raise instagram.InstagramError("HTTP 500: boom")
        return "MID"

    with TmpState():
        with patch.object(instagram, "list_conversations",
                          lambda t, u: threads), \
             patch.object(instagram, "send_message", send):
            n, perm = engagement.answer_dms(CFG, st, BANK, TODAY)
        assert n == 1 and perm is None
        assert st["dm_replied"] == ["T_OK"]


# ------------------------------------------------------------------- helpers
@test
def parse_time_shapes():
    for good in ("2026-09-04T10:00:00+0000", "2026-09-04T10:00:00Z",
                 "2026-09-04 10:00:00", "2026-09-04T10:00:00+00:00"):
        t = engagement._parse_time(good)
        assert t is not None and t.tzinfo is not None, good
    assert engagement._parse_time("garbage") is None
    assert engagement._parse_time(None) is None
    assert engagement._parse_time("") is None


@test
def participant_igsid_found():
    own = engagement._own_usernames(BANK, CFG)
    th = {"users": [{"username": "whoisaaniiiya", "id": "OWN"},
                    {"username": "fan", "id": "IG_9"}]}
    assert engagement._participant_igsid(th, own) == "IG_9"
    only_us = {"users": [{"username": "@WhoIsAaniiiya", "id": "OWN"}]}
    assert engagement._participant_igsid(only_us, own) is None
    assert engagement._participant_igsid({}, own) is None


@test
def recent_media_ids_newest_first_dedup_limited():
    st = {"days": {
        "2026-09-01": {"published_media": {"feed": "OLD1"}},
        "2026-09-04": {"published_media": {"feed": "NEW", "reel": "R",
                                            "story": "S"}},
        "2026-09-03": {"published_media": {"feed": "NEW"}},   # dup across days
    }}
    assert engagement._recent_media_ids(st, limit=2) == ["NEW", "R"]
    # Stories are excluded on purpose (no comments edge — querying one 400s
    # with Graph's generic permission-sounding error).
    assert engagement._recent_media_ids(st) == ["NEW", "R", "OLD1"]
    assert engagement._recent_media_ids({"days": {}}) == []


# ------------------------------------------------------------------- alerting
@test
def evaluate_engagement_open_and_close():
    st = {"engagement": {"permissions_missing": ["messages", "comments"]}}
    a = alerts.evaluate_engagement(st)
    assert a["open"] and a["title"] == alerts.ENGAGEMENT_PERMS_TITLE
    assert "instagram_manage_comments" in a["body"]
    assert "instagram_business_manage_messages" in a["body"]
    ok = alerts.evaluate_engagement(
        {"engagement": {"permissions_missing": []}})
    assert not ok["open"] and ok["resolve_note"]
    blank = alerts.evaluate_engagement({})
    assert not blank["open"]
    # recovery closes: a run that works clears the record -> issue closes
    st["engagement"]["permissions_missing"] = []
    assert not alerts.evaluate_engagement(st)["open"]


@test
def alerts_evaluate_includes_engagement_condition():
    st = {"days": {}, "engagement": {"permissions_missing": ["comments"]}}
    out = alerts.evaluate(st, TODAY, None)
    eng = [a for a in out if a["title"] == alerts.ENGAGEMENT_PERMS_TITLE]
    assert len(eng) == 1 and eng[0]["open"]


# ---------------------------------------------------------------- entry point
@test
def run_smoke_writes_engagement_record():
    cfg = dict(CFG, gh_pat="")
    st = fresh_state()
    with TmpState():
        # run() loads state from the tmp file, so seed it with media ids
        state.save(st)
        with patch.object(engagement, "load_config", lambda: cfg), \
             patch.object(instagram, "list_media", lambda t, u, limit=50: []), \
             patch.object(instagram, "list_comments", lambda t, m: []), \
             patch.object(instagram, "list_conversations",
                          lambda t, u: []):
            engagement.run()
        loaded = state.load()
        assert loaded["engagement"]["permissions_missing"] == []
        assert "0 comment replies, 0 DM replies" == \
            loaded["engagement"]["last_run"]


# ---------------------------------------------------------------------- runner
def main():
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
    sys.exit(main())
