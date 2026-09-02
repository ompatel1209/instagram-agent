"""Orchestrator: generate -> render -> upload (media branch) -> publish.

Modes:
  --dry-run     render locally into preview/ only; no API calls at all
  --prepare     render + push media branch, but do not publish
  (default)     full automatic run: render, push, publish both, update state
"""
import argparse
import datetime as dt
import subprocess
import sys
import time

import requests as _requests

from . import content, instagram, render, state, token
from .config import PREVIEW_DIR, ROOT, load_config, media_url


def log(msg: str) -> None:
    print(f"[ig-agent] {msg}", flush=True)


def build_media(cfg: dict, date: dt.date, out_dir) -> dict:
    """Pick content + render both JPGs; returns paths and selection."""
    quotes, tips = content.load_banks()
    picked = content.pick(quotes, tips, date)
    feed_path = out_dir / f"{picked['date']}-feed.jpg"
    story_path = out_dir / f"{picked['date']}-story.jpg"
    render.render_feed(picked["quote"], cfg["palette"], cfg["handle"], feed_path)
    render.render_story(picked["tip"], cfg["palette"], cfg["handle"], story_path)
    return {"picked": picked, "feed": feed_path, "story": story_path}


def push_media(cfg: dict, date_str: str) -> tuple[bool, bool]:
    """Push rendered JPGs to the public `media` branch via the shell script,
    then poll raw.githubusercontent.com until both files return HTTP 200 with
    an image content-type (Meta's servers fetch image_url when the container
    is created, so the files must be live before publishing starts)."""
    subprocess.run(["bash", "scripts/push_media.sh", date_str], check=True)

    def fetchable(url: str) -> bool:
        for _ in range(12):  # 12 x 10s = ~2 min worst case
            try:
                r = _requests.get(url, timeout=20)
                if r.status_code == 200 and "image" in r.headers.get(
                        "content-type", ""):
                    return True
            except _requests.RequestException:
                pass
            time.sleep(10)
        return False

    return (fetchable(media_url(cfg, date_str, "feed")),
            fetchable(media_url(cfg, date_str, "story")))


def run() -> int:
    parser = argparse.ArgumentParser(prog="ig-agent")
    parser.add_argument("--dry-run", action="store_true",
                        help="render to preview/ only; no API calls, no pushes")
    parser.add_argument("--prepare", action="store_true",
                        help="render + push media branch; skip publishing")
    parser.add_argument("--date", default=None,
                        help="override date as YYYY-MM-DD (testing)")
    args = parser.parse_args()

    cfg = load_config()
    IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
    date = (dt.date.fromisoformat(args.date) if args.date
            else dt.datetime.now(IST).date())

    # --- Dry-run: render only ------------------------------------------------
    if args.dry_run:
        result = build_media(cfg, date, PREVIEW_DIR)
        picked = result["picked"]
        log(f"dry-run ok — feed: {result['feed']}")
        log(f"dry-run ok — story: {result['story']}")
        log(f"quote: {picked['quote']['text'][:60]}…")
        log(f"tip: {picked['tip']['title']}")
        return 0

    # --- Full run needs secrets ---------------------------------------------
    if not cfg["access_token"]:
        log("ERROR: IG_ACCESS_TOKEN secret is not set — skipping run.")
        return 0  # exit 0 so a not-yet-configured repo doesn't show failures
    if not cfg["repo_owner"] or not cfg["repo_name"]:
        log("ERROR: repo_owner/repo_name missing from config.json — cannot "
            "build public media URLs.")
        return 1

    token_str = cfg["access_token"]
    ig_user_id = cfg["ig_user_id"] or instagram.get_user_id(token_str)

    st = state.load()
    date_str = date.isoformat()

    # Idempotency: nothing left to do means the run is complete.
    if state.both_published(st, date_str):
        log(f"{date_str}: feed + story already published — nothing to do.")
        return 0

    # --- Render + push media --------------------------------------------------
    out_dir = ROOT / "media_out"
    result = build_media(cfg, date, out_dir)
    picked = result["picked"]
    log(f"content: quote#N tip '{picked['tip']['title']}'")

    feed_url = media_url(cfg, date_str, "feed")
    story_url = media_url(cfg, date_str, "story")

    if args.prepare:
        log(f"prepare mode — media rendered to {out_dir}; not publishing.")
        return 0

    # Push JPGs to the public `media` branch NOW, before creating containers —
    # Meta's servers fetch image_url at container creation, so the files must
    # already be downloadable when create_container() runs.
    ok_feed, ok_story = push_media(cfg, date_str)
    if not (ok_feed and ok_story):
        log("ERROR: media not downloadable from raw.githubusercontent.com — "
            "cannot create containers. Skipping publish.")
        state.note_failure(st, date_str, "media_push",
                           "media files not downloadable after push")
        state.save(st)
        return 1
    log("media pushed and verified downloadable from media branch")

    # --- Token housekeeping (never blocks publishing) -------------------------
    new_token, secret_updated = token.refresh_and_store(
        token_str, cfg["gh_pat"],
        f"{cfg['repo_owner']}/{cfg['repo_name']}",
    )
    token_str = new_token

    # --- Publish feed ----------------------------------------------------------
    if not state.done(st, date_str, "publish_feed"):
        try:
            cid = instagram.create_container(
                token_str, ig_user_id, feed_url,
                caption=content.caption_for(picked["quote"], cfg["hashtags"]),
            )
            instagram.wait_finished(token_str, cid)
            mid = instagram.publish(token_str, ig_user_id, cid)
            state.record_media_id(st, date_str, "feed", mid)
            state.mark(st, date_str, "publish_feed")
            state.save(st)
            log(f"feed published: {mid}")
        except instagram.InstagramError as e:
            log(f"feed publish FAILED: {e}")
            state.note_failure(st, date_str, "feed", e)
            state.save(st)

    # --- Publish story ----------------------------------------------------------
    if not state.done(st, date_str, "publish_story"):
        try:
            cid = instagram.create_container(
                token_str, ig_user_id, story_url, media_type="STORIES",
            )
            instagram.wait_finished(token_str, cid)
            mid = instagram.publish(token_str, ig_user_id, cid)
            state.record_media_id(st, date_str, "story", mid)
            state.mark(st, date_str, "publish_story")
            state.save(st)
            log(f"story published: {mid}")
        except instagram.InstagramError as e:
            log(f"story publish FAILED: {e}")
            state.note_failure(st, date_str, "story", e)
            state.save(st)

    # --- Token expiry bookkeeping ----------------------------------------------
    try:
        d_left, iso = token.days_left(token_str)
        if d_left is not None:
            state.note_token_expiry(st, iso, d_left)
            if d_left < 7:
                log(f"WARNING: token expires in {d_left}d ({iso}) — refresh "
                    "the IG_ACCESS_TOKEN secret manually if it wasn't auto-updated.")
            state.save(st)
    except Exception:
        pass

    # --- Exit code reflects completeness ---------------------------------------
    st = state.load()
    if state.both_published(st, date_str):
        log(f"{date_str}: complete (feed + story published).")
        return 0
    log(f"{date_str}: incomplete — safety re-run will retry missing steps.")
    return 1


if __name__ == "__main__":
    sys.exit(run())
