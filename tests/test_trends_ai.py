"""Tests for live trend terms + AI hashtags (src/trends.py, src/ai.py).

Runs with plain python3 — no pytest, no network, no real key. A stub
`requests` module is injected BEFORE src imports, so the RSS fetch and the
NIM completion are fully faked: we queue a FakeResponse (or an exception)
and inspect the recorded call.

Covers:
  - RSS parse: lowercased, deduped, capped at the limit
  - live_terms: [] on network error and on non-200 (never raises)
  - trending.sanitize: charset, blocked tags, dedupe, cap
  - generate_hashtags: JSON array, ``` fenced array, prose fallback,
    empty key, no live terms, truncated reasoning (finish_reason != stop)
  - generate_hashtags: usage lands in state's ai_tokens_spent row
  - the prompt actually carries the live terms (the model is shown the
    trend, it is never asked to recall one)

Run:  python3 tests/test_trends_ai.py     (exit 0 = all passed)
"""
import json
import pathlib
import sys
import types

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


class _RequestException(Exception):
    pass


CALLS = {"get": [], "post": []}
GET = {"response": None, "exc": False}
POST = {"response": None, "exc": False}


def _get(url, **kwargs):
    CALLS["get"].append((url, kwargs))
    if GET["exc"]:
        raise _RequestException("network down")
    return GET["response"]


def _post(url, **kwargs):
    CALLS["post"].append((url, kwargs))
    if POST["exc"]:
        raise _RequestException("network down")
    return POST["response"]


_requests = types.ModuleType("requests")
_requests.RequestException = _RequestException
_requests.get = _get
_requests.post = _post
sys.modules["requests"] = _requests

# ------------------------------------------------------------ imports under test
from src import ai, trending, trends  # noqa: E402
from src.config import ist_today  # noqa: E402

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item><title>Navratri Garba</title></item>
  <item><title>navratri garba</title></item>
  <item><title>Delhi Fashion Week</title></item>
  <item><title>IPL</title></item>
</channel></rss>"""

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def _reset():
    CALLS["get"].clear()
    CALLS["post"].clear()
    GET.update(response=None, exc=False)
    POST.update(response=None, exc=False)


def _completion(content, finish="stop", tokens=120):
    return FakeResponse(payload={
        "choices": [{"finish_reason": finish,
                     "message": {"content": content}}],
        "usage": {"total_tokens": tokens},
    })


# -------------------------------------------------------------- src/trends.py
@test
def rss_parse_lowercases_dedupes_and_caps():
    terms = trends._parse(RSS, limit=3)
    assert terms == ["navratri garba", "delhi fashion week", "ipl"], terms


@test
def live_terms_returns_empty_on_network_error():
    _reset()
    GET["exc"] = True
    assert trends.live_terms() == []


@test
def live_terms_returns_empty_on_non_200():
    _reset()
    GET["response"] = FakeResponse(status_code=429, text="")
    assert trends.live_terms() == []


@test
def live_terms_parses_a_real_payload():
    _reset()
    GET["response"] = FakeResponse(text=RSS)
    assert "navratri garba" in trends.live_terms()


# ------------------------------------------------------------ trending.sanitize
@test
def sanitize_rejects_junk_banned_and_dupes():
    raw = ["#Cute", "cute", "followforfollow", "two words", "a",
           "x" * 31, "valid_tag", "emoji✨"]
    assert trending.sanitize(raw) == ["cute", "valid_tag"]


@test
def sanitize_caps_the_list():
    assert len(trending.sanitize([f"tag{i}" for i in range(40)])) == 30
    assert len(trending.sanitize([f"tag{i}" for i in range(40)], cap=5)) == 5


# ------------------------------------------------------------- ai.generate_hashtags
@test
def generate_hashtags_parses_a_json_array():
    _reset()
    POST["response"] = _completion('["Navratri", "garba", "ootd"]')
    tags = ai.generate_hashtags("festive", ["navratri"], api_key="k")
    assert tags == ["Navratri", "garba", "ootd"], tags


@test
def generate_hashtags_tolerates_fenced_json_and_prose():
    _reset()
    POST["response"] = _completion('Here you go:\n```json\n["aesthetic"]\n```')
    assert ai.generate_hashtags("cute", ["x"], api_key="k") == ["aesthetic"]

    _reset()
    POST["response"] = _completion("Try #delhifashion and #OOTD today")
    assert ai.generate_hashtags("ootd", ["x"], api_key="k") == \
        ["delhifashion", "OOTD"]


@test
def generate_hashtags_needs_a_key_and_live_terms():
    _reset()
    assert ai.generate_hashtags("cute", ["x"], api_key="") is None
    assert ai.generate_hashtags("cute", [], api_key="k") is None
    assert CALLS["post"] == [], "no model call without a key or a trend"


@test
def generate_hashtags_none_on_truncated_reasoning():
    _reset()
    POST["response"] = _completion('["cute"]', finish="length")
    assert ai.generate_hashtags("cute", ["x"], api_key="k") is None


@test
def generate_hashtags_none_on_http_error():
    _reset()
    POST["response"] = FakeResponse(status_code=500, payload={})
    assert ai.generate_hashtags("cute", ["x"], api_key="k") is None


@test
def generate_hashtags_shows_the_live_terms_to_the_model():
    _reset()
    POST["response"] = _completion('["navratri"]')
    ai.generate_hashtags("festive", ["navratri garba", "ipl final"],
                         caption="garba night", api_key="k")
    body = CALLS["post"][0][1]["json"]
    prompt = body["messages"][1]["content"]
    assert "navratri garba" in prompt and "ipl final" in prompt
    assert body["model"] == ai.MODEL


@test
def generate_hashtags_records_token_spend():
    _reset()
    st = {}
    POST["response"] = _completion('["cute"]', tokens=321)
    ai.generate_hashtags("cute", ["x"], api_key="k", st=st)
    assert st["ai_tokens_spent"][ist_today().isoformat()] == 321


# ---------------------------------------------------------------------- runner
def run_tests():
    failed = []
    for fn in TESTS:
        _reset()
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
