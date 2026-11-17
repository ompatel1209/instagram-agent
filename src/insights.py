"""Daily insights + weekly digest: the account's growth numbers, collected.

Every publishing run also reads, via the official Graph API (the token's
existing instagram_business_basic permission — no extra scope):
  - the account's followers_count / media_count that day;
  - reach / likes / comments / saved for each post from the last 7 days
    that hasn't been measured yet (feed + Reels only — Stories expire and
    their insights die with them).

It all lands in state.json under "insights" -> "days" (capped at 35 days so
the public file never grows without limit), and on Mondays run_daily()
writes a WEEKLY DIGEST into the run log + state.json: 7-day totals,
follower delta, and average reach per caption vibe — which is what turns
"we post daily" into "post more of what the audience actually reaches".

Design contracts (the whole repo's style, applied to metrics):
  - Non-fatal: any API failure is a failure note, never a crash, and never
    affects the day's publish result. A broken insights feature must never
    cost a post.
  - Idempotent: measured media ids are never re-fetched (each post is
    measured once, ~24h after publishing — metrics fetched minutes after a
    post would read ~0 forever and waste the only read we get).
  - Bounded: day rows capped (KEEP_DAYS), unmeasurable ids capped, every
    failure message redacted through note_failure like everywhere else.

Run:  python3 -m src.insights   (manual one-shot; the publishing run
      calls run_daily() automatically)
"""
import datetime as dt
import time

from . import captions as captions_mod
from . import instagram, state

# Safe metric set for BOTH feed images and Reels (probed live 2026-11-17:
# both IMAGE and VIDEO accept reach/likes/comments/saved on
# graph.instagram.com). Requesting a metric a media type doesn't support
# 400s the whole call, so the set stays small.
METRICS = "reach,likes,comments,saved"

KEEP_DAYS = 35          # insight day-rows kept in state.json
RECENT_DAYS = 7         # how far back unmeasured posts are still fetched
MAX_FETCH_PER_RUN = 6   # bound one run's insight reads (first runs backfill)
PACING_S = 1.5          # gap between insight reads — a 6-call burst right
                        # after publishing got every call rate-rejected once
ATTEMPTS_BEFORE_DEAD = 3  # failed reads before an id stops being retried
UNMEASURABLE_CAP = 50   # dead ids we must not retry forever


def _log(msg: str) -> None:
    print(f"[ig-agent] {msg}", flush=True)


def collect(cfg: dict, st: dict, date_str: str) -> None:
    """One day's collection: followers + unmeasured post metrics.

    Non-fatal and idempotent; saves state when (and only when) something
    changed. Never raises into the caller.
    """
    token = cfg["access_token"]
    ig_user_id = cfg["ig_user_id"]

    try:
        rec = st.setdefault("insights", {})
        days = rec.setdefault("days", {})
        day_rec = days.setdefault(date_str, {})
        changed = False

        # --- account stats ------------------------------------------------
        try:
            stats = instagram.get_account_stats(token, ig_user_id)
            followers = stats.get("followers_count")
            if isinstance(followers, int):
                if day_rec.get("followers") != followers:
                    day_rec["followers"] = followers
                    changed = True
            if isinstance(stats.get("media_count"), int):
                day_rec["media_count"] = stats["media_count"]
        except instagram.InstagramError as e:
            state.note_failure(st, date_str, "insights",
                               f"account stats failed — {e}")

        # --- unmeasured posts from the last RECENT_DAYS --------------------
        # Today's own ids are skipped: metrics read within minutes of
        # publishing are ~0 forever, and the id would never be re-fetched.
        # "unmeasurable" maps media id -> failed-read count (older versions
        # stored a flat list — coerced here): an id stops being retried only
        # after ATTEMPTS_BEFORE_DEAD failures, so a transient rate-limit
        # burst never writes off a healthy post.
        raw_dead = rec.get("unmeasurable")
        dead = dict(raw_dead) if isinstance(raw_dead, dict) else (
            {str(k): ATTEMPTS_BEFORE_DEAD for k in raw_dead}
            if isinstance(raw_dead, list) else {})
        measured = _measured_ids(days)
        pending: list[tuple[str, str]] = []
        for dstr in sorted(st.get("days", {}).keys(), reverse=True):
            if dstr >= date_str or len(pending) >= MAX_FETCH_PER_RUN:
                continue
            pm = st["days"][dstr].get("published_media")
            if not isinstance(pm, dict):
                continue
            for kind in ("feed", "reel"):
                mid = pm.get(kind)
                if (mid and str(mid) not in measured
                        and dead.get(str(mid), 0) < ATTEMPTS_BEFORE_DEAD):
                    pending.append((dstr, str(mid)))

        for i, (dstr, mid) in enumerate(pending):
            if i:  # pace the reads — bursts get rate-rejected (see PACING_S)
                time.sleep(PACING_S)
            try:
                m = instagram.media_insights(token, mid, METRICS)
            except instagram.InstagramError as e:
                # A dead id (post deleted, story expired) must not produce
                # a failure note every run for the rest of its life — but a
                # single transient failure must not write it off either.
                attempts = dead.get(mid, 0) + 1
                dead[mid] = attempts
                if attempts >= ATTEMPTS_BEFORE_DEAD:
                    _log(f"insights: {mid} not measurable after "
                         f"{attempts} attempts — giving up ({e})")
                else:
                    _log(f"insights: {mid} read failed (attempt "
                         f"{attempts}/{ATTEMPTS_BEFORE_DEAD}) — {e}")
                state.note_failure(st, date_str, "insights",
                                   f"insights for {mid} failed — {e}")
                changed = True
                continue
            dead.pop(mid, None)
            days.setdefault(dstr, {}).setdefault("media", {})[mid] = m
            changed = True
            _log(f"insights: {dstr} {mid} — "
                 + ", ".join(f"{k}={v}" for k, v in sorted(m.items())))

        if rec.get("unmeasurable") != dead or dead:
            # keep the capped id->attempts map only when non-empty
            if dead:
                if len(dead) > UNMEASURABLE_CAP:
                    for k in sorted(dead, key=dead.get)[:-UNMEASURABLE_CAP]:
                        dead.pop(k, None)
                rec["unmeasurable"] = dead
                changed = True
            elif "unmeasurable" in rec:
                del rec["unmeasurable"]
                changed = True

        # --- retention -----------------------------------------------------
        if len(days) > KEEP_DAYS:
            for k in sorted(days)[:-KEEP_DAYS]:
                days.pop(k)
            changed = True

        if changed:
            state.save(st)
    except Exception as e:  # metrics must never kill a run
        try:
            state.note_failure(st, date_str, "insights",
                               f"collection crashed — {e}")
            state.save(st)
        except Exception:
            pass
        _log(f"insights: collection failed — {e}")


def _measured_ids(days: dict) -> set[str]:
    """Every media id insights have already been fetched for."""
    out: set[str] = set()
    for v in days.values():
        media = v.get("media") if isinstance(v, dict) else None
        if isinstance(media, dict):
            out.update(str(k) for k in media)
    return out


# --- Weekly digest --------------------------------------------------------------


def weekly_digest(st: dict, date: dt.date) -> dict | None:
    """The Monday digest, or None when there is no week of data yet.

    Pure computation over collected insights: 7-day totals, follower
    delta, and average reach per caption vibe (which posts carried which
    vibe comes from each day's media_of_day filename)."""
    days = st.get("insights", {}).get("days", {})
    if not isinstance(days, dict):
        return None
    start = (date - dt.timedelta(days=6)).isoformat()
    week = [(d, v) for d, v in sorted(days.items()) if start <= d]
    if not week:
        return None

    totals = {"reach": 0, "likes": 0, "comments": 0, "saved": 0}
    posts = 0
    vibe_reach: dict[str, list[int]] = {}
    for dstr, v in week:
        media = v.get("media") if isinstance(v, dict) else None
        if not isinstance(media, dict):
            continue
        filename = st.get("days", {}).get(dstr, {}).get("media_of_day")
        vibe = (captions_mod.vibe_from_filename(filename)
                if filename else "general")
        for mid, m in media.items():
            if not isinstance(m, dict):
                continue
            posts += 1
            for k in totals:
                totals[k] += int(m.get(k) or 0)
            if isinstance(m.get("reach"), int):
                vibe_reach.setdefault(vibe, []).append(m["reach"])

    followers = [v.get("followers") for _, v in week
                 if isinstance(v.get("followers"), int)]
    delta = (followers[-1] - followers[0]) if len(followers) >= 2 else None

    best_vibe = None
    if vibe_reach:
        ranked = sorted(vibe_reach.items(),
                        key=lambda kv: sum(kv[1]) / len(kv[1]), reverse=True)
        best_vibe, best_avg = ranked[0][0], sum(ranked[0][1]) / len(ranked[0][1])
    else:
        best_avg = None

    return {
        "week_start": week[0][0], "week_end": week[-1][0],
        "posts_measured": posts,
        **totals,
        "followers_start": followers[0] if followers else None,
        "followers_end": followers[-1] if followers else None,
        "followers_delta": delta,
        "best_vibe": best_vibe,
        "best_vibe_avg_reach": int(best_avg) if best_avg is not None else None,
        "vibe_reach": {k: sum(v) // len(v) for k, v in vibe_reach.items()},
    }


def _digest_log(d: dict) -> None:
    _log(f"weekly digest ({d['week_start']} → {d['week_end']}): "
         f"{d['posts_measured']} post(s) — reach {d['reach']}, "
         f"likes {d['likes']}, comments {d['comments']}, saved {d['saved']}")
    if d.get("followers_delta") is not None:
        _log(f"followers: {d['followers_start']} → {d['followers_end']} "
             f"({'+' if d['followers_delta'] >= 0 else ''}"
             f"{d['followers_delta']})")
    if d.get("best_vibe"):
        _log(f"best vibe this week: {d['best_vibe']} "
             f"(avg reach {d['best_vibe_avg_reach']})")


def run_daily(cfg: dict, date_str: str, enabled: bool = True) -> None:
    """Entry point from main.run()'s finally: collect + Monday digest.

    Never raises and never touches publish state's day records beyond its
    own "insights" block. enabled=False (--prepare) means no API calls.
    """
    if not enabled or not cfg.get("access_token") or not cfg.get("ig_user_id"):
        return
    try:
        st = state.load()
        collect(cfg, st, date_str)
        date = dt.date.fromisoformat(date_str)
        rec = st.get("insights", {})
        if (date.weekday() == 0  # Monday
                and rec.get("last_digest") != date_str):
            digest = weekly_digest(st, date)
            if digest:
                _digest_log(digest)
                rec["last_digest"] = date_str
                rec["digest"] = digest
                state.save(st)
            else:
                _log("weekly digest: not enough data yet — skipped")
    except Exception as e:
        _log(f"insights: run_daily failed — {e}")


if __name__ == "__main__":
    from .config import load_config
    from .config import ist_today
    cfg = load_config()
    run_daily(cfg, ist_today().isoformat())
