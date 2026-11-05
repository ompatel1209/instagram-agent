"""Daily self-update refresh: fresh trending tags + the day's content plan.

Feature 2's last piece. Runs in its own workflow (.github/workflows/
trending.yml, 03:03 UTC = 8:33 AM IST) before the 9:03 IST post, so the
day's captions already carry the freshly-rotated trending window.

Trend sourcing is layered. The IG Graph API exposes no hashtag-trending
endpoint for this token (ig_hashtag_search 400s, subcode 33), so live
signal comes from Google Trends' public daily RSS (src/trends): NIM maps
today's searches onto the day's vibe, then the curated pool in
content/hashtags.json fills the rest. No AI key, no live terms, or a
model failure = the deterministic rotation, exactly as before.

Three steps, each non-fatal (a failed step is noted in state and the rest
still run — this module must never raise into the workflow):
  1. Log the day's plan: uploads-queue depth + next file (vibe, photo vs
     video), the day's music track, and the IG token's days-left.
  2. Stage a fresh "trending_now" window into content/hashtags.json —
     the hook trending.pick_trending reads FIRST, ahead of the day's
     vibe/global rotations. Runs after step 1 because the window is
     matched to the queued post's vibe.
  3. Record the run's stats as top-level st["refresh"] (overwritten
     daily, so state.json never grows from this). A failed queue lookup
     is recorded as queue_remaining: null — never 0, because a failed
     lookup must not look like an empty queue (alerting reads the queue
     independently of these stats, so they are informational only).
"""
import datetime as dt
import json
import os
import sys

from . import ai
from . import captions as captions_mod
from . import festivals, music, state, trending, trends, uploads

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# hashtags.json keys that are not vibe pools (the window draws one tag
# from each vibe pool; every other key in the file is preserved as-is).
NON_VIBE_KEYS = {"_readme", "trending_now", "global"}

# Staged window size. trending.pick_trending surfaces only the first 3;
# the extra slack keeps the rotation varied without bloating the file.
TRENDING_NOW_COUNT = 5


def _today() -> dt.date:
    """The run's date: POST_DATE_OVERRIDE env, else now in IST.

    Mirrors src/main.py's date computation (same timezone, same env
    override, same future-date refusal) so a backfill refreshes the tags
    for the date that actually publishes, and the 03:03 UTC cron stages
    the window for the same IST day the 03:33 UTC post will use.
    """
    override = os.environ.get("POST_DATE_OVERRIDE", "")
    if override:
        from .main import resolve_date
        return resolve_date(override)
    return dt.datetime.now(IST).date()


def _write_pool(pool: dict) -> None:
    with open(trending.CONTENT_DIR / "hashtags.json", "w",
              encoding="utf-8") as f:
        json.dump(pool, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _stage_trending_now(date: dt.date, st: dict | None = None,
                        vibe: str = "general") -> list[str]:
    """Stage the day's trending_now window into content/hashtags.json.

    AI first: today's live searches (src/trends) are mapped onto this
    post's vibe by NIM (src/ai.generate_hashtags); the top 3 lead the
    window and the curated rotation fills the rest, so the caption never
    depends on one source. AI off, no live terms, or a model failure ->
    the rotation alone, identical to the pre-AI behavior.

    Idempotent per date: when state is passed and a window is already
    recorded for the day it is reused verbatim, so a re-run never rewrites
    the day's caption (the model is non-deterministic, the rotation was not).

    Raises only on a missing/corrupt pool file — the caller notes it and
    the previous window stays in place (a stale window beats a broken file).
    """
    date_str = date.isoformat()
    rec = state.day(st, date_str) if st is not None else {}
    existing = rec.get("trending_now")
    if isinstance(existing, list) and existing:
        pool = trending.load_pool()
        pool["trending_now"] = existing
        _write_pool(pool)
        return existing

    pool = trending.load_pool()
    day = date.toordinal() - trending.EPOCH.toordinal()
    vibes = [k for k in pool
             if k not in NON_VIBE_KEYS and trending._clean(pool.get(k))]
    if vibes:
        lead = day % len(vibes)
        vibes = vibes[lead:] + vibes[:lead]
    window: list[str] = []
    for name in vibes[:TRENDING_NOW_COUNT - 1]:
        window += trending._rotate(trending._clean(pool.get(name)), 1,
                                   f"{date.isoformat()}:now:{name}", day)
    window += trending._rotate(trending._clean(pool.get("global")), 1,
                               f"{date.isoformat()}:now:global", day)
    window = trending._clean(window)

    terms = trends.live_terms()
    ai_tags: list[str] = []
    if terms:
        try:
            ai_tags = trending.sanitize(
                ai.generate_hashtags(
                    vibe, terms,
                    captions_mod.compose_caption(vibe, date) or "",
                    count=TRENDING_NOW_COUNT, st=st),
                cap=TRENDING_NOW_COUNT)
        except Exception as e:  # belt and braces: generate_hashtags can't raise
            if st is not None:
                state.note_failure(st, date_str, "refresh",
                                   f"AI hashtags failed — {e}")
    source = "ai" if ai_tags else "rotation"
    window = trending._clean(ai_tags[:3] + window)[:TRENDING_NOW_COUNT]

    pool["trending_now"] = window
    _write_pool(pool)
    if st is not None:
        rec["trending_now"] = window
        rec["trending_source"] = source
        state.save(st)  # persist the idempotency guard before the slow steps
    return window


def run() -> int:
    date = _today()
    date_str = date.isoformat()
    print(f"refresh: {date_str} — rotating trending tags for the day")

    st = state.load()
    rec: dict = {
        "date": date_str,
        "trending_now": None,
        "trending_source": None,
        "queue_remaining": None,
        "next_file": None,
        "vibe": None,
        "is_video": None,
        "music_track": None,
        "token_days_left": None,
        "festival": None,
    }

    # --- 1) the day's plan ----------------------------------------------------
    try:
        queue = uploads.list_queue_checked()
    except Exception as e:
        queue = None
        state.note_failure(st, date_str, "refresh",
                           f"queue lookup failed — {e}")
    if queue is None:
        # Unknown queue (failed lookup) is NOT an empty queue — recorded
        # as null so nothing downstream can misread it as "0 files left".
        state.note_failure(st, date_str, "refresh",
                           "uploads-queue lookup failed — depth unknown")
        print("refresh: uploads-queue lookup failed — depth unknown")
    else:
        posted = set(state.posted_files(st))
        remaining = [f for f in queue if f not in posted]
        rec["queue_remaining"] = len(remaining)
        if remaining:
            nxt = remaining[0]
            rec["next_file"] = nxt
            rec["vibe"] = captions_mod.vibe_from_filename(nxt)
            rec["is_video"] = captions_mod.is_video(nxt)
            kind = "video (Reel)" if rec["is_video"] else "photo (feed)"
            print(f"refresh: uploads queue — {len(remaining)} file(s) left, "
                  f"next: {nxt} [{rec['vibe']}, {kind}]")
        else:
            print("refresh: uploads queue empty — stock/quote tiers "
                  "fill the day")

    # --- 2) fresh trending_now window (after the queue: vibe is known) ------
    try:
        window = _stage_trending_now(date, st, rec.get("vibe") or "general")
        rec["trending_now"] = window
        rec["trending_source"] = state.day(
            st, date_str).get("trending_source", "rotation")
        if window:
            head = ", ".join(f"#{t}" for t in window[:3])
            print(f"refresh: trending_now staged "
                  f"({rec['trending_source']}) — {head} …")
        else:
            print("refresh: trending pools empty — window staged empty")
    except Exception as e:
        state.note_failure(st, date_str, "refresh",
                           f"trending_now refresh failed — {e}")
        print(f"refresh: trending_now refresh failed — {e}")

    try:
        track = music.pick_track(date)
        if track:
            rec["music_track"] = {"id": track["id"], "mood": track["mood"]}
            print(f"refresh: music pick — {track['id']} ({track['mood']})")
    except Exception as e:
        state.note_failure(st, date_str, "refresh", f"music plan failed — {e}")

    # Festival day-plan line: informational (the calendar's vibe is a hint,
    # the day's media keeps its own); the greeting + tags flow through
    # main.py's caption assembly regardless of this note.
    try:
        fest = festivals.festival_for(date)
        if fest:
            rec["festival"] = {"id": fest.get("id"),
                               "name": fest.get("name"),
                               "vibe": fest.get("vibe")}
            print(f"refresh: festival — {fest['name']} "
                  f"({fest.get('vibe', 'general')}) — greeting + tags "
                  f"lead today's captions")
    except Exception as e:
        state.note_failure(st, date_str, "refresh", f"festival plan failed — {e}")

    try:
        days = st.get("token", {}).get("days_left")
        if days is not None:
            rec["token_days_left"] = days
            print(f"refresh: IG token — {days} day(s) left")
    except Exception:
        pass  # informational only — nothing worth a failure note

    # --- 3) record + persist ---------------------------------------------------
    st["refresh"] = rec
    state.save(st)
    print(f"refresh: done — plan recorded for {date_str}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
