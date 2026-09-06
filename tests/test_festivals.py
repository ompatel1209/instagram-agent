"""Tests for src/festivals.py + the festival caption hooks.

Runs with plain python3 — no network, no PIL, no requests: stub modules are
injected BEFORE src imports (the test_trending.py pattern). src.festivals
is pure-python (json + random), but the import chains under test pull
render->PIL and requests transitively, so stub them.

Covers the festival-calendar contract:
  - fixed entries match their MM-DD every year; lunar entries match only
    the dates explicitly mapped in their "years" map
  - a day with no entry is a normal day (None — never an error)
  - missing/corrupt calendar degrades to no-festival (never raises)
  - the greeting line is deterministic per (date, id) and always comes
    from the entry's own lines
  - first matching entry wins a same-day collision (majors listed first)
  - festival tags are cleaned (#/case/dedupe) and ride right behind the
    static vibe bank in trending.caption_tags, with BLOCKED honored
  - the uploads/quote-tier hook: greet() leads the caption, the day's
    body and tag line follow unchanged
  - main._publish_image_pair derives its date from date_str locally (the
    historical NameError — free-variable `date` — must stay fixed)

Run:  python3 tests/test_festivals.py     (exit 0 = all passed)
"""
import datetime as dt
import json
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
from src import captions as captions_mod  # noqa: E402
from src import content, festivals, main, trending  # noqa: E402
from src.config import CONTENT_DIR  # noqa: E402

# Ganesh Chaturthi 2026 (lunar, mapped); Independence Day (fixed); a plain
# mid-September day with no entry anywhere in the shipped calendar.
FEST_DATE = dt.date(2026, 9, 14)
FIXED_DATE = dt.date(2026, 8, 15)
PLAIN_DATE = dt.date(2026, 9, 7)
BANK = captions_mod.load_bank()

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ------------------------------------------------------------------ lookups
@test
def calendar_loads_with_entries():
    entries = festivals._load()
    assert len(entries) >= 19, f"expected the shipped calendar, got {len(entries)}"
    assert all(e.get("id") and e.get("name") for e in entries)


@test
def fixed_date_matches_every_year():
    for year in (2026, 2027, 2028):
        fest = festivals.festival_for(dt.date(year, 8, 15))
        assert fest and fest["id"] == "independence-day", \
            f"Independence Day must match in {year}"


@test
def lunar_date_matches_mapped_year():
    fest = festivals.festival_for(FEST_DATE)
    assert fest and fest["id"] == "ganesh-chaturthi"


@test
def lunar_date_non_mapped_year_is_none():
    # Holi maps 2026-2028 only; 2029 has no entry, so it's a plain day.
    assert festivals.festival_for(dt.date(2029, 3, 22)) is None


@test
def plain_day_is_none():
    assert festivals.festival_for(PLAIN_DATE) is None


@test
def first_match_wins_on_collision():
    cal = {"festivals": [
        {"id": "major", "name": "Major Fest", "date": "07-07",
         "vibe": "cute", "lines": ["major line"], "tags": ["majorfest"]},
        {"id": "minor", "name": "Minor Fest", "date": "07-07",
         "vibe": "ootd", "lines": ["minor line"], "tags": ["minorfest"]},
    ]}
    path = CONTENT_DIR / "festivals.json"
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text(json.dumps(cal), encoding="utf-8")
        fest = festivals.festival_for(dt.date(2026, 7, 7))
        assert fest and fest["id"] == "major", \
            "first matching entry must win — keep majors listed first"
    finally:
        path.write_text(original, encoding="utf-8")


# -------------------------------------------------------------- failure modes
@test
def corrupt_calendar_degrades_to_none():
    path = CONTENT_DIR / "festivals.json"
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text("{ not json !!", encoding="utf-8")
        # Today's a festival day — but a corrupt calendar means a normal day.
        assert festivals.festival_for(FEST_DATE) is None
    finally:
        path.write_text(original, encoding="utf-8")


@test
def missing_calendar_degrades_to_none():
    path = CONTENT_DIR / "festivals.json"
    original = path.read_text(encoding="utf-8")
    path.unlink()
    try:
        assert festivals.festival_for(FEST_DATE) is None
        assert festivals.festival_line(None, FEST_DATE) == ""
        assert festivals.festival_tags(None) == []
    finally:
        path.write_text(original, encoding="utf-8")


# ------------------------------------------------------------------- the line
@test
def festival_line_is_deterministic_and_from_the_entry():
    fest = festivals.festival_for(FEST_DATE)
    a = festivals.festival_line(fest, FEST_DATE)
    b = festivals.festival_line(fest, FEST_DATE)
    assert a == b and a, "same (date, id) must greet identically"
    assert a in fest["lines"], "greeting must come from the entry's lines"
    # A different date re-seeds — over the entry's 3 lines the pick moves.
    picks = {festivals.festival_line(fest, FEST_DATE + dt.timedelta(days=i))
             for i in range(30)}
    assert len(picks) > 1, "line choice should vary across dates"


@test
def festival_tags_cleaned_and_deduped():
    fest = {"id": "test", "tags": ["#Party", "party", " Party ", "", "Fest"]}
    assert festivals.festival_tags(fest) == ["party", "fest"]


# --------------------------------------------------------------- greet() shape
@test
def greet_prefixes_the_line():
    fest = festivals.festival_for(FEST_DATE)
    line = festivals.festival_line(fest, FEST_DATE)
    greeted = festivals.greet("body caption", fest, FEST_DATE)
    assert greeted == f"{line}\n\nbody caption"
    assert greeted.split("\n")[0] == line, "greeting must LEAD the caption"


@test
def greet_is_noop_without_festival():
    assert festivals.greet("body caption", None, PLAIN_DATE) == "body caption"


@test
def greet_is_noop_when_lines_missing():
    fest = {"id": "lineless", "name": "Lineless", "date": "07-07", "tags": []}
    assert festivals.greet("body caption", fest, PLAIN_DATE) == "body caption"


# ------------------------------------------------------ tag merge in trending
@test
def festival_tags_ride_behind_static_bank():
    fest = festivals.festival_for(FEST_DATE)
    ftags = festivals.festival_tags(fest)
    assert ftags, "shipped festival entries carry tags"
    static = BANK["cute"]["hashtags"]
    merged = trending.caption_tags(
        "cute", FEST_DATE, static, [], fest_tags=ftags)
    # Static bank keeps its lead block...
    assert merged[:len(static)] == static
    # ...festival tags all survive...
    assert set(ftags) <= set(merged), "festival tags must all be present"
    # ...and the first tag after the static block is a festival tag.
    tail = merged[len(static):]
    assert not tail or tail[0] in ftags, \
        f"festival tags must lead the trending window: {tail}"


@test
def blocked_festival_tags_dropped_in_merge():
    cal = {"festivals": [
        {"id": "spammy", "name": "Spammy", "date": FEST_DATE.isoformat()[5:],
         "vibe": "cute", "lines": ["x"],
         "tags": ["followme", "uniquefesttag"]},
    ]}
    path = CONTENT_DIR / "festivals.json"
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text(json.dumps(cal), encoding="utf-8")
        fest = festivals.festival_for(FEST_DATE)
        merged = trending.caption_tags(
            "cute", FEST_DATE, BANK["cute"]["hashtags"], [],
            fest_tags=festivals.festival_tags(fest))
        assert "followme" not in merged, "BLOCKED must win over the calendar"
        assert "uniquefesttag" in merged
    finally:
        path.write_text(original, encoding="utf-8")


# ------------------------------------------------------- the tier hooks
@test
def uploads_tier_hook_greeting_leads_caption():
    # The exact main.run_upload_day assembly: composed line (bank fallback)
    # + greet + caption_tags with the fest_tags slot.
    fest = festivals.festival_for(FEST_DATE)
    picked = captions_mod.pick(BANK, "cute", FEST_DATE)
    line = captions_mod.compose_caption("cute", FEST_DATE) or picked["caption"]
    tags = trending.caption_tags(
        "cute", FEST_DATE, picked["hashtags"], [],
        fest_tags=festivals.festival_tags(fest))
    cap = captions_mod.format_caption(festivals.greet(line, fest, FEST_DATE),
                                      tags)
    assert cap.split("\n")[0] == festivals.festival_line(fest, FEST_DATE)
    assert line in cap, "the day's caption body must follow the greeting"
    assert cap.rstrip().split("\n")[-1].startswith("#")


@test
def quote_tier_hook_greeting_leads_caption():
    # The exact main._publish_image_pair assembly: caption_for(quote, tags)
    # wrapped in greet, with the fest_tags slot filled.
    fest = festivals.festival_for(FEST_DATE)
    quote_tags = trending.caption_tags(
        "general", FEST_DATE, BANK["general"]["hashtags"], [],
        fest_tags=festivals.festival_tags(fest))
    body = content.caption_for({"text": "Stay golden.", "author": "Aani"},
                               quote_tags)
    cap = festivals.greet(body, fest, FEST_DATE)
    assert cap == f"{festivals.festival_line(fest, FEST_DATE)}\n\n{body}"


@test
def publish_image_pair_has_local_date():
    # _publish_image_pair historically crashed on a free-variable `date`
    # (only run() ever defined it). The fix derives it from date_str —
    # assert it's a real local of the function so the NameError can't return.
    code = main._publish_image_pair.__code__
    assert "date" in code.co_varnames, \
        "date must be a local (derived from date_str), not a free/global name"
    assert "date_str" in code.co_varnames
    assert "fest" in code.co_varnames, "quote tier must look up the festival"


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
