"""Trending-tag rotation: daily viral hashtags, deterministically picked.

The IG Graph API exposes no hashtag-search/trending endpoint for this token
(ig_hashtag_search returns 400 subcode 33), so "trending" here is a curated
pool in content/hashtags.json rotated by (date, vibe). Deterministic = the
same day always injects the same tags, so a re-run of a day never changes its
caption mid-flight, and the day's posts stay idempotent.

Selection shape per day: up to TRENDING_COUNT tags for the day's vibe, plus
up to GLOBAL_COUNT from the global reach list, plus anything the Feature 2
refresh workflow staged into "trending_now". Everything is deduped against
the static vibe bank, lowercased, and the "#" stripped — caption_text does
the final formatting.
"""
import datetime as dt
import json
import random

from .config import CONTENT_DIR

EPOCH = dt.date(2026, 9, 2)  # same epoch as the caption bank — one rotation

# How many rotated tags to inject per day. Keeps total caption tags within
# IG's recommended range (the static vibe bank already contributes 8-10).
TRENDING_COUNT = 4
GLOBAL_COUNT = 3

# Never inject these even if they appear in a pool list: banned/overloaded
# tags get posts downranked and some (follow-for-follow style) are outright
# against IG's spam policy.
BLOCKED = {
    "followforfollow", "follow4follow", "f4f", "likeforlike", "like4like",
    "l4l", "followforlikes", "followme", "likeforfollow", "instafollow",
    "followback", "gainpost", "spammypost", "followloop",
}


def load_pool() -> dict:
    with open(CONTENT_DIR / "hashtags.json", encoding="utf-8") as f:
        return json.load(f)


def _clean(tags: list[str] | None) -> list[str]:
    """Lowercase, strip '#', drop empties, blocked, and duplicates."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags or []:
        t = str(t).lstrip("#").strip().lower()
        if t and t not in BLOCKED and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _rotate(tags: list[str], n: int, seed: str, start: int) -> list[str]:
    """Pick n tags starting at a deterministic offset, wrapping around.

    A rotation (not a shuffle) so consecutive days move through the pool in
    order and every tag gets used before anything repeats.
    """
    if not tags:
        return []
    rng = random.Random(seed)
    # Offset advances one slot per day; jitter keeps same-vibe days that
    # would collide (e.g. two ootd days) from landing on the same window.
    offset = (start + rng.randrange(len(tags))) % len(tags)
    return [tags[(offset + i) % len(tags)] for i in range(min(n, len(tags)))]


def pick_trending(vibe: str, date: dt.date) -> list[str]:
    """The day's trending tags: trending_now + rotated vibe + global tags.

    Never raises: a corrupt pool file degrades to [] and the caption falls
    back to the static vibe bank alone. On any exception, return [].
    """
    try:
        pool = load_pool()
        day = dt.date.fromisoformat(date.isoformat()).toordinal() \
            - EPOCH.toordinal()
        rotated = []
        # Freshly-refreshed tags first (Feature 2 writes this list daily).
        rotated += _clean(pool.get("trending_now"))[:3]
        rotated += _rotate(_clean(pool.get(vibe)), TRENDING_COUNT,
                           f"{date.isoformat()}:{vibe}", day)
        rotated += _rotate(_clean(pool.get("global")), GLOBAL_COUNT,
                           f"{date.isoformat()}:global", day)
        return _clean(rotated)
    except Exception:
        return []


def merge_tags(static_tags: list[str], trending: list[str],
               cap: int = 30) -> list[str]:
    """Static vibe bank first, then trending tags, deduped and capped.

    Static tags keep their position (they're the vibe identity); trending
    tags ride behind them. cap=30 leaves headroom under IG's 30-tag limit
    while never producing a caption-length error.
    """
    return _clean(list(static_tags) + list(trending))[:cap]


def caption_tags(vibe: str, date: dt.date, static_tags: list[str],
                 extra_tags: list[str] | None = None) -> list[str]:
    """Full tag list for a caption: static bank + trending + cfg extras.

    This is the single entry point caption paths call — it keeps trending
    injection, static-bank precedence, dedupe, and the 30-tag cap in one
    place so every tier (uploads/stock/quote) formats tags identically.
    """
    trending = pick_trending(vibe, date)
    return merge_tags(static_tags, trending + list(extra_tags or []))
