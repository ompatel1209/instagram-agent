"""Tests for the AI reply voice (src/ai.py) + its engagement wiring.

Runs with plain python3 — no pytest, no network, no PIL. A stub `requests`
module is injected BEFORE src imports so the NIM call is fully faked: we
queue FakeResponse objects (or Exceptions) and inspect the recorded call.
No test here needs a real API key: the empty-key path and every failure
mode must degrade to the reply bank without raising.

Covers the Feature 3 contract:
  - generate_reply returns None on: empty key, HTTP != 200, finish_reason
    "length" (reasoning truncated), empty content, network exception
  - a successful completion returns the content; usage lands in
    state.json ai_tokens_spent (today's row) and history is capped at 7
    day-rows
  - the daily token cap blocks the model before a call is made
  - DM history is built from the thread's messages (oldest-first, own
    messages as assistant, the answered message excluded)
  - personality_for: love -> romantic, friendship -> friendly, general
    seeds rotate friendly/funny/emotional deterministically
  - tidy(): comment kind collapses newlines; both kinds cap at 2200
  - the engagement wiring: AI success sends the tidied model reply (bank
    untouched), AI None falls back to the bank pick, empty key in cfg ->
    bank reply identical to the pre-AI behavior

Run:  python3 tests/test_ai.py     (exit 0 = all passed)
"""
import datetime as dt
import json
import os
import pathlib
import sys
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
# A stray NVIDIA_API_KEY in this shell must not change outcomes here.
os.environ.pop("NVIDIA_API_KEY", None)
from src import ai, engagement, instagram, state  # noqa: E402

BANK = engagement.load_bank()
TODAY = dt.date.today().isoformat()
CFG = {"access_token": "TOK", "ig_user_id": "IG_USER",
       "handle": "@whoisaaniiiya"}
CFG_AI = dict(CFG, nvidia_api_key="NIMKEY")

TESTS = []


def test(fn):
    def wrapped():
        # isolation: calls/responses from earlier tests must not leak in —
        # the "never call NIM" assertions read _fake.calls.
        _fake.calls.clear()
        _fake.responses.clear()
        return fn()

    wrapped.__name__ = fn.__name__
    TESTS.append(wrapped)
    return fn


# ------------------------------------------------------------------- fixtures
def fresh_state():
    return {"days": {
        "2026-09-04": {"published_media": {"feed": "M_FEED",
                                           "story": "M_STORY"}},
        "2026-09-03": {"published_media": {"reel": "M_REEL"}},
    }}


def all_live(st):
    """Patch list_media so every recorded feed/reel id reads as still on
    the account — reconciliation then changes nothing."""
    ids = engagement._recent_media_ids(st)
    return patch.object(
        instagram, "list_media",
        lambda t, u, limit=50: [{"id": i} for i in ids])


class TmpState:
    """Context: state.save/load go to a throwaway file, saves are counted."""

    def __enter__(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self._p1 = patch.object(
            state, "STATE_PATH",
            pathlib.Path(self._dir.name) / "state.json")
        self._p1.start()
        real_save = state.save
        self.saves = []

        def counting(st_obj):
            self.saves.append(1)
            real_save(st_obj)

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


def nim_ok(content="sure! here for you 🤍", tokens=500):
    """A queued successful NIM completion with the given content/usage."""
    _fake.responses.append(FakeResponse(payload={
        "choices": [{"finish_reason": "stop",
                     "message": {"content": content,
                                 "reasoning_content": "thinking..."}}],
        "usage": {"total_tokens": tokens},
    }))


def nim_fail(content=None, finish="stop", status=200, tokens=500):
    """A queued NIM response exercising a failure mode."""
    _fake.responses.append(FakeResponse(status_code=status, payload={
        "choices": [{"finish_reason": finish,
                     "message": {"content": content}}],
        "usage": {"total_tokens": tokens},
    }))


# ------------------------------------------------------------- generate_reply
@test
def empty_key_returns_none():
    _fake.responses.clear()
    assert ai.generate_reply("friendly", "auto", "hi",
                             api_key="", st=None) is None
    assert ai.generate_reply("friendly", "auto", "hi",
                             api_key=None, st=None) is None
    assert not _fake.calls, "no key must mean no HTTP call at all"


@test
def http_error_returns_none():
    _fake.responses.clear()
    _fake.responses.append(FakeResponse(status_code=500))
    assert ai.generate_reply("friendly", "auto", "hi",
                             api_key="K", st=None) is None


@test
def truncated_reasoning_returns_none():
    _fake.responses.clear()
    nim_fail(content=None, finish="length")
    assert ai.generate_reply("friendly", "auto", "hi",
                             api_key="K", st=None) is None


@test
def empty_content_returns_none():
    _fake.responses.clear()
    nim_fail(content="", finish="stop")
    assert ai.generate_reply("friendly", "auto", "hi",
                             api_key="K", st=None) is None


@test
def network_exception_returns_none():
    _fake.responses.clear()
    _fake.responses.append(RuntimeError("connection reset"))
    assert ai.generate_reply("friendly", "auto", "hi",
                             api_key="K", st=None) is None


@test
def success_returns_content_and_spends():
    _fake.responses.clear()
    nim_ok(content="hey you! 🤍", tokens=500)
    st = fresh_state()
    assert ai.generate_reply("friendly", "auto", "hi",
                             api_key="K", st=st) == "hey you! 🤍"
    assert st[ai.AI_BUDGET_STATE_KEY][TODAY] == 500
    # second call accumulates
    nim_ok(content="again", tokens=300)
    ai.generate_reply("friendly", "auto", "hi", api_key="K", st=st)
    assert st[ai.AI_BUDGET_STATE_KEY][TODAY] == 800
    # the request shape: model, budget, temperature, and the system prompt
    method, url, kw = _fake.calls[-1]
    assert url.endswith("/chat/completions") and method == "POST"
    body = kw["json"]
    assert body["model"] == ai.MODEL
    assert body["max_tokens"] == ai.MAX_TOKENS
    assert body["messages"][0]["role"] == "system"
    assert "whoisaaniiiya" in body["messages"][0]["content"]


@test
def budget_cap_blocks_before_any_call():
    _fake.responses.clear()
    st = fresh_state()
    st[ai.AI_BUDGET_STATE_KEY] = {
        TODAY: ai.AI_DAILY_TOKEN_CAP,
        "2026-09-01": 1,
    }
    assert ai.generate_reply("friendly", "auto", "hi",
                             api_key="K", st=st) is None
    assert not _fake.calls, "cap reached must mean no HTTP call"


@test
def spend_history_keeps_newest_seven_days():
    st = fresh_state()
    # 11 old day-rows, no today row yet
    st[ai.AI_BUDGET_STATE_KEY] = {
        (dt.date.today() - dt.timedelta(days=d)).isoformat(): 10 * d
        for d in range(1, 12)}
    ai._spend_tokens(st, 50)
    rows = st[ai.AI_BUDGET_STATE_KEY]
    assert len(rows) == 7
    assert rows[TODAY] == 50
    # kept rows are today + the six most recent prior days
    expected = {TODAY} | {
        (dt.date.today() - dt.timedelta(days=d)).isoformat()
        for d in range(1, 7)}
    assert set(rows) == expected


@test
def history_capped_at_memory_turns():
    _fake.responses.clear()
    nim_ok()
    long_history = [("user", f"m{i}") for i in range(20)]
    ai.generate_reply("friendly", "auto", "now",
                      history=long_history, api_key="K", st=None)
    body = _fake.calls[-1][2]["json"]
    # system + newest 6 history turns + the new message
    assert len(body["messages"]) == 1 + ai.MEMORY_TURNS + 1
    assert body["messages"][-1]["content"] == "now"
    assert body["messages"][1]["content"] == "m14"  # newest-6 window


# ------------------------------------------------------------ personality_for
@test
def personality_category_mapping():
    assert ai.personality_for("any-seed", "love") == "romantic"
    assert ai.personality_for("any-seed", "friendship") == "friendly"
    assert ai.personality_for("tid", None) in ("friendly", "funny", "emotional")


@test
def personality_general_rotation_is_deterministic():
    a = ai.personality_for("thread-1", "general")
    b = ai.personality_for("thread-1", "general")
    assert a == b and a in ("friendly", "funny", "emotional")
    # across many seeds all three surface
    picks = {ai.personality_for(f"s{i}", "general") for i in range(30)}
    assert picks == {"friendly", "funny", "emotional"}


# --------------------------------------------------------------------- tidy
@test
def tidy_comment_collapses_newlines():
    assert ai.tidy("  hello \n world \n\n from ai  ", "comment") == \
        "hello world from ai"


@test
def tidy_caps_at_2200():
    long = "x" * 3000
    trimmed = ai.tidy(long, "dm")
    assert len(trimmed) == 2198 and trimmed.endswith("…")
    assert len(ai.tidy("y" * 2199)) == 2199  # under the cap: untouched


# ---------------------------------------------------- _thread_history helper
@test
def dm_history_roles_and_order():
    own = {"whoisaaniiiya"}
    msgs = [
        msg("m1", "whoisaaniiiya", "hii 🤍", iso(5)),
        msg("m2", "fan_guy", "hii, main tumhe pasand karta hoon", iso(4)),
        msg("m3", "whoisaaniiiya", "aww that's so sweet!", iso(3)),
        msg("m4", "fan_guy", "kya tum mere saath dosti karogi?", iso(2)),
    ]
    last = msgs[-1]
    history = engagement._thread_history(msgs, last, own)
    assert history == [
        ("assistant", "hii 🤍"),
        ("user", "hii, main tumhe pasand karta hoon"),
        ("assistant", "aww that's so sweet!"),
    ]


@test
def dm_history_excludes_empty_texts():
    own = {"whoisaaniiiya"}
    msgs = [
        msg("m1", "fan_guy", "", iso(3)),   # empty text dropped
        msg("m2", "fan_guy", "hello!", iso(2)),
    ]
    history = engagement._thread_history(msgs, msgs[-1], own)
    assert history == []


# -------------------------------------------- engagement wiring: comments
@test
def comment_ai_success_sends_tidied_model_reply():
    _fake.responses.clear()
    st = fresh_state()
    sent = []
    with TmpState():
        with all_live(st):
            with patch.object(instagram, "list_comments",
                              lambda t, mid: [
                                  {"id": "C1", "username": "fan1",
                                   "text": "so beautiful"}]):
                with patch.object(instagram, "reply_to_comment",
                                  lambda t, cid, txt: sent.append(txt)):
                    nim_ok(content="  thank you so much! "
                                   "line1\nline2  ", tokens=100)
                    n, perm = engagement.reply_to_comments(
                        CFG_AI, st, BANK, "2026-09-04")
    assert n == 1 and perm is None and len(sent) == 1
    assert sent[0] == "thank you so much! line1 line2", sent
    assert st[ai.AI_BUDGET_STATE_KEY][TODAY] == 100


@test
def comment_ai_none_falls_back_to_bank():
    _fake.responses.clear()
    st = fresh_state()
    sent = []
    with TmpState():
        with all_live(st):
            with patch.object(instagram, "list_comments",
                              lambda t, mid: [
                                  {"id": "C1", "username": "fan1",
                                   "text": "so beautiful"}]):
                with patch.object(instagram, "reply_to_comment",
                                  lambda t, cid, txt: sent.append(txt)):
                    nim_fail(content=None, finish="length")
                    n, perm = engagement.reply_to_comments(
                        CFG_AI, st, BANK, "2026-09-04")
    assert n == 1 and len(sent) == 1
    # the bank fallback: the deterministic comment_replies pick for C1
    expected = engagement._pick_replies(
        engagement.categorize(BANK, "so beautiful"),
        "comment_replies", "C1")[0]
    assert sent[0] == expected


@test
def comment_no_key_uses_bank_and_never_calls_nim():
    _fake.responses.clear()
    st = fresh_state()
    sent = []
    with TmpState():
        with all_live(st):
            with patch.object(instagram, "list_comments",
                              lambda t, mid: [
                                  {"id": "C1", "username": "fan1",
                                   "text": "so beautiful"}]):
                with patch.object(instagram, "reply_to_comment",
                                  lambda t, cid, txt: sent.append(txt)):
                    n, perm = engagement.reply_to_comments(
                        CFG, st, BANK, "2026-09-04")
    assert n == 1 and not _fake.calls, \
        "empty nvidia_api_key must not touch the NIM endpoint"
    expected = engagement._pick_replies(
        engagement.categorize(BANK, "so beautiful"),
        "comment_replies", "C1")[0]
    assert sent[0] == expected


@test
def comment_ai_success_spends_into_real_state():
    # AI usage lands in state.json's ai_tokens_spent for the day (saved
    # via the per-reply state.save inside the flow).
    _fake.responses.clear()
    st = fresh_state()
    with TmpState():
        with all_live(st):
            with patch.object(instagram, "list_comments",
                              lambda t, mid: [
                                  {"id": "C2", "username": "fan2",
                                   "text": "nice pic"}]):
                with patch.object(instagram, "reply_to_comment",
                                  lambda t, cid, txt: None):
                    nim_ok(tokens=222)
                    engagement.reply_to_comments(
                        CFG_AI, st, BANK, "2026-09-04")
        saved = state.load()
    assert saved[ai.AI_BUDGET_STATE_KEY][TODAY] == 222


# -------------------------------------------- engagement wiring: DMs
@test
def dm_ai_success_sends_model_reply_with_history():
    _fake.responses.clear()
    st = fresh_state()
    sent = []
    msgs = [
        msg("m1", "whoisaaniiiya", "hey guys!!", iso(5)),
        msg("m2", "fan_guy", "hi!! you're so pretty", iso(4)),
        msg("m3", "whoisaaniiiya", "aww thank you 🤍", iso(3)),
        msg("m4", "fan_guy", "want to be friends?", iso(2)),
    ]
    with TmpState():
        with patch.object(instagram, "list_conversations",
                          lambda t, u: [thread("T1", "fan_guy", "IG1", msgs)]):
            with patch.object(instagram, "send_message",
                              lambda t, u, s, txt: sent.append(txt)):
                nim_ok(content="yesss I'd love that! 🤍", tokens=150)
                n, perm = engagement.answer_dms(CFG_AI, st, BANK, "2026-09-04")
    assert n == 1 and perm is None and len(sent) == 1
    assert sent[0] == "yesss I'd love that! 🤍"
    assert st[ai.AI_BUDGET_STATE_KEY][TODAY] == 150
    # the model saw the thread's prior turns as memory
    body = _fake.calls[-1][2]["json"]
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "assistant", "user", "assistant", "user"]
    assert body["messages"][-1]["content"] == "want to be friends?"


@test
def dm_ai_none_falls_back_to_bank():
    _fake.responses.clear()
    st = fresh_state()
    sent = []
    msgs = [msg("m1", "fan_guy", "hi!! love your posts", iso(2))]
    with TmpState():
        with patch.object(instagram, "list_conversations",
                          lambda t, u: [thread("T1", "fan_guy", "IG1", msgs)]):
            with patch.object(instagram, "send_message",
                              lambda t, u, s, txt: sent.append(txt)):
                nim_fail(content=None, finish="length")
                n, perm = engagement.answer_dms(CFG_AI, st, BANK, "2026-09-04")
    assert n == 1 and len(sent) == 1
    expected = engagement._pick_replies(
        engagement.categorize(BANK, "hi!! love your posts"),
        "dm_replies", "T1")[0]
    assert sent[0] == expected


@test
def dm_no_key_uses_bank_and_never_calls_nim():
    _fake.responses.clear()
    st = fresh_state()
    sent = []
    msgs = [msg("m1", "fan_guy", "hi!! love your posts", iso(2))]
    with TmpState():
        with patch.object(instagram, "list_conversations",
                          lambda t, u: [thread("T1", "fan_guy", "IG1", msgs)]):
            with patch.object(instagram, "send_message",
                              lambda t, u, s, txt: sent.append(txt)):
                n, perm = engagement.answer_dms(CFG, st, BANK, "2026-09-04")
    assert n == 1 and not _fake.calls
    expected = engagement._pick_replies(
        engagement.categorize(BANK, "hi!! love your posts"),
        "dm_replies", "T1")[0]
    assert sent[0] == expected


@test
def dm_budget_cap_falls_back_to_bank():
    _fake.responses.clear()
    st = fresh_state()
    st[ai.AI_BUDGET_STATE_KEY] = {TODAY: ai.AI_DAILY_TOKEN_CAP}
    sent = []
    msgs = [msg("m1", "fan_guy", "hi!! love your posts", iso(2))]
    with TmpState():
        with patch.object(instagram, "list_conversations",
                          lambda t, u: [thread("T1", "fan_guy", "IG1", msgs)]):
            with patch.object(instagram, "send_message",
                              lambda t, u, s, txt: sent.append(txt)):
                n, perm = engagement.answer_dms(
                    CFG_AI, st, BANK, "2026-09-04")
    assert n == 1 and not _fake.calls, "cap reached must not call NIM"
    expected = engagement._pick_replies(
        engagement.categorize(BANK, "hi!! love your posts"),
        "dm_replies", "T1")[0]
    assert sent[0] == expected


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
