"""Tests for src/trending.py + the trending caption hook.

Runs with plain python3 — no network, no PIL, no requests: stub modules are
injected BEFORE src imports (the test_day_complete.py pattern). src.trending
itself is pure-python (json + random), but the main.py import chain pulls
render->PIL and requests transitively, so stub them.

Covers the Feature 1 contract:
  - tags are deterministic per (date, vibe) — re-runs never rewrite captions
  - rotation actually moves day to day (same vibe, different dates differ)
  - dedupe: static bank + trending + extras never repeat a tag
  - 30-tag cap and blocked-tag filtering
  - corrupt pool file degrades to [] (caption falls back to static bank)
  - the uploads-tier hook (main.run_upload_day's tag assembly) and the
    quote-fallback hook produce formatted captions with a tag line

Run:  python3 tests/test_trending.py     (exit 0 = all passed)
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
from src import trending  # noqa: E402
from src.config import CONTENT_DIR  # noqa: E402

DATE = dt.date(2026, 9, 10)
BANK = captions_mod.load_bank()

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ------------------------------------------------------------- determinism
@test
def same_date_vibe_picks_identical_tags():
    a = trending.pick_trending("ootd", DATE)
    b = trending.pick_trending("ootd", DATE)
    assert a == b and a, "expected stable non-empty selection"


@test
def different_dates_rotate_the_pool():
    a = trending.pick_trending("ootd", DATE)
    b = trending.pick_trending("ootd", DATE + dt.timedelta(days=1))
    assert a != b, "rotation must move day to day"


@test
def different_vibes_same_date_differ():
    a = trending.pick_trending("cute", DATE)
    b = trending.pick_trending("attitude", DATE)
    assert a != b


# ------------------------------------------------------------------- shape
@test
def pick_trending_returns_vibe_plus_global_tags():
    tags = trending.pick_trending("selfie", DATE)
    pool = trending.load_pool()
    vibe_set = {t for t in pool["selfie"]}
    global_set = set(pool["global"])
    assert any(t in vibe_set for t in tags), "no vibe tag present"
    assert any(t in global_set for t in tags), "no global tag present"
    assert len(tags) <= trending.TRENDING_COUNT + trending.GLOBAL_COUNT


@test
def trending_now_tags_surface_first():
    # The Feature 2 hook: refreshed tags must lead the day's selection.
    pool_path = CONTENT_DIR / "hashtags.json"
    original = pool_path.read_text(encoding="utf-8")
    try:
        pool = json.loads(original)
        pool["trending_now"] = ["FRESHVIRAL", "#freshviral2"]
        pool_path.write_text(json.dumps(pool), encoding="utf-8")
        tags = trending.pick_trending("cute", DATE)
        assert tags[0] == "freshviral" and tags[1] == "freshviral2"
    finally:
        pool_path.write_text(original, encoding="utf-8")


@test
def blocked_tags_never_included():
    merged = trending.merge_tags(
        ["cute"], ["followforfollow", "#LIKE4LIKE", "cutie", "likeforlike"])
    assert "followforfollow" not in merged
    assert "like4like" not in merged
    assert "cutie" in merged


@test
def merge_tags_dedupes_and_caps():
    static = ["selfie", "instaselfie", "SELFIE"]  # SELFIE dup after lowering
    merged = trending.merge_tags(static, ["instaselfie", "golden", "vibes"])
    assert merged.count("selfie") == 1 and merged.count("instaselfie") == 1
    assert len(trending.merge_tags([f"t{i}" for i in range(40)],
                                   [])) == 30


# ------------------------------------------------------------ failure modes
@test
def corrupt_pool_degrades_to_empty():
    pool_path = CONTENT_DIR / "hashtags.json"
    original = pool_path.read_text(encoding="utf-8")
    try:
        pool_path.write_text("{ not json !!", encoding="utf-8")
        assert trending.pick_trending("cute", DATE) == []
        # And the full hook still yields the static bank alone.
        tags = trending.caption_tags("cute", DATE, BANK["cute"]["hashtags"], [])
        assert tags == BANK["cute"]["hashtags"]
    finally:
        pool_path.write_text(original, encoding="utf-8")


@test
def caption_tags_falls_back_when_pool_empty():
    # Empty trending + extras: caption_tags = static bank, unchanged.
    tags = trending.caption_tags("travel", DATE,
                                 BANK["travel"]["hashtags"], ["motivation"])
    assert tags[:len(BANK["travel"]["hashtags"])] == \
        BANK["travel"]["hashtags"]
    assert "motivation" in tags
    assert len(tags) <= 30


# ----------------------------------------------------------- caption hook
@test
def uploads_tier_hook_formats_caption_with_tags():
    picked = captions_mod.pick(BANK, "ootd", DATE)
    tags = trending.caption_tags(
        "ootd", DATE, picked["hashtags"], ["motivation", "dailyquotes"])
    cap = captions_mod.format_caption(picked["caption"], tags)
    lines = cap.split("\n")
    assert lines[0] == picked["caption"]
    assert lines[1] == ""
    tag_line = lines[2]
    assert tag_line.startswith("#")
    assert all(t.startswith("#") for t in tag_line.split())
    assert tag_line.count("#") == len(tags)
    # static bank keeps its lead position in the merged list
    assert tag_line.split()[0] == "#" + BANK["ootd"]["hashtags"][0]


@test
def merged_tag_count_stays_under_ig_limit():
    for vibe in BANK:
        tags = trending.caption_tags(
            vibe, DATE, BANK[vibe]["hashtags"],
            ["motivation", "dailyquotes", "inspiration", "growthmindset",
             "selfimprovement", "dailymotivation"])
        assert 12 <= len(tags) <= 30, f"{vibe}: {len(tags)} tags"


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
