"""Orchestrator: pick media -> render/pad -> upload (media branch) -> publish.

Modes:
  --dry-run     render locally into preview/ only; no API calls at all
  --prepare     render + push media branch, but do not publish
  (default)     full automatic run: render, push, publish both, update state

Two content sources:
  - uploads queue: the user's own photos/videos on the `media` branch
    (uploads/<file>, one per day; photos -> feed + Story, videos -> Reels +
    Story). Used whenever the queue has an unposted file.
  - quote/tip fallback: rendered gradient graphics, used when the queue is
    empty so the daily schedule never goes silent.
"""
import argparse
import datetime as dt
import os
import pathlib
import subprocess
import sys
import time

import requests as _requests

from . import alerts, captions as captions_mod
from . import content, instagram, pexels, render, state, token, uploads
from .config import PREVIEW_DIR, ROOT, load_config, media_url


def log(msg: str) -> None:
    print(f"[ig-agent] {msg}", flush=True)


# --- Quote/tip fallback (original flow) ------------------------------------

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

    def fetchable(url: str, is_image: bool = True) -> bool:
        for _ in range(12):  # 12 x 10s = ~2 min worst case
            try:
                r = _requests.get(url, timeout=20)
                if r.status_code == 200:
                    kind = r.headers.get("content-type", "")
                    if (is_image and "image" in kind) or (
                            not is_image and "video" in kind):
                        return True
            except _requests.RequestException:
                pass
            time.sleep(10)
        return False

    return (fetchable(media_url(cfg, date_str, "feed")),
            fetchable(media_url(cfg, date_str, "story")))


# --- Uploads-queue flow (user's own photos/videos) ---------------------------

def _download(url: str, dest: pathlib.Path) -> bool:
    """Download a queued upload to the runner for padding/cover extraction."""
    try:
        r = _requests.get(url, timeout=120)
        if r.status_code == 200:
            dest.write_bytes(r.content)
            return True
    except _requests.RequestException:
        pass
    return False


def run_upload_day(cfg: dict, date: dt.date, date_str: str, st: dict,
                   out_dir: pathlib.Path) -> int:
    """Publish one queued user file: photo -> feed + Story, video -> Reel +
    Story (cover frame). Returns 0 on success, 1 on failure (retryable)."""
    already = state.media_of_day(st, date_str)
    if already:
        filename = already
        log(f"resuming upload day with {filename} (idempotent re-run)")
    else:
        filename = uploads.next_file(state.posted_files(st))
    if not filename:
        log("uploads queue empty")
        return -1  # signal caller to fall back to quote/tip flow

    state.set_media_of_day(st, date_str, filename)
    state.save(st)

    vibe = captions_mod.vibe_from_filename(filename)
    is_video = captions_mod.is_video(filename)
    bank = captions_mod.load_bank()
    caption = captions_mod.caption_text(bank, vibe, date, cfg["hashtags"])
    log(f"upload: {filename} (vibe: {vibe}, {'video' if is_video else 'photo'})")

    src_url = uploads.raw_url(filename, cfg)
    src_path = out_dir / "queue-source.bin"
    out_dir.mkdir(parents=True, exist_ok=True)  # _download writes here first
    if not _download(src_url, src_path):
        log(f"ERROR: could not download {filename} from media branch")
        state.note_failure(st, date_str, "upload_download",
                           f"could not fetch {filename}")
        state.save(st)
        return 1

    ok_feed = ok_story = False

    if is_video:
        # --- Video: Reel + Story cover frame --------------------------------
        reel_url = src_url
        story_cover = out_dir / f"{date_str}-story.jpg"
        cover = render.extract_cover_frame(src_path, story_cover)
        if cover:
            subprocess.run(["bash", "scripts/push_file.sh",
                            str(story_cover), f"{date_str}-story.jpg"],
                          check=True)
            def fetchable(url: str) -> bool:
                for _ in range(12):
                    try:
                        r = _requests.get(url, timeout=20)
                        if r.status_code == 200 and "image" in r.headers.get(
                                "content-type", ""):
                            return True
                    except _requests.RequestException:
                        pass
                    time.sleep(10)
                return False
            ok_story = fetchable(media_url(cfg, date_str, "story"))

        if not state.done(st, date_str, "publish_feed"):
            try:
                cid = instagram.create_container(
                    cfg["access_token"], cfg["ig_user_id"], video_url=reel_url,
                    media_type="REELS", caption=caption,
                )
                instagram.wait_finished(cfg["access_token"], cid,
                                       max_wait_s=600, poll_s=30)
                mid = instagram.publish(cfg["access_token"], cfg["ig_user_id"], cid)
                state.record_media_id(st, date_str, "feed", mid)
                state.mark(st, date_str, "publish_feed")
                state.save(st)
                log(f"reel published: {mid}")
                ok_feed = True
            except instagram.InstagramError as e:
                log(f"reel publish FAILED: {e}")
                state.note_failure(st, date_str, "feed", e)
                state.save(st)

        if ok_story and not state.done(st, date_str, "publish_story"):
            try:
                cid = instagram.create_container(
                    cfg["access_token"], cfg["ig_user_id"],
                    image_url=media_url(cfg, date_str, "story"),
                    media_type="STORIES",
                )
                instagram.wait_finished(cfg["access_token"], cid)
                mid = instagram.publish(cfg["access_token"], cfg["ig_user_id"], cid)
                state.record_media_id(st, date_str, "story", mid)
                state.mark(st, date_str, "publish_story")
                state.save(st)
                log(f"story published: {mid}")
            except instagram.InstagramError as e:
                log(f"story publish FAILED: {e}")
                state.note_failure(st, date_str, "story", e)
                state.save(st)

    else:
        # --- Photo: padded feed post + Story ---------------------------------
        feed_path = out_dir / f"{date_str}-feed.jpg"
        story_path = out_dir / f"{date_str}-story.jpg"
        render.render_upload_feed(src_path, cfg["palette"], feed_path)
        render.render_upload_story(src_path, cfg["palette"], story_path)

        ok_feed, ok_story = push_media(cfg, date_str)
        if not (ok_feed and ok_story):
            log("ERROR: padded media not downloadable — cannot publish.")
            state.note_failure(st, date_str, "media_push",
                               "padded media not downloadable after push")
            state.save(st)
            return 1
        log("media padded and verified downloadable from media branch")

        if not state.done(st, date_str, "publish_feed"):
            try:
                cid = instagram.create_container(
                    cfg["access_token"], cfg["ig_user_id"],
                    image_url=media_url(cfg, date_str, "feed"), caption=caption,
                )
                instagram.wait_finished(cfg["access_token"], cid)
                mid = instagram.publish(cfg["access_token"], cfg["ig_user_id"], cid)
                state.record_media_id(st, date_str, "feed", mid)
                state.mark(st, date_str, "publish_feed")
                state.save(st)
                log(f"feed published: {mid}")
            except instagram.InstagramError as e:
                log(f"feed publish FAILED: {e}")
                state.note_failure(st, date_str, "feed", e)
                state.save(st)

        if not state.done(st, date_str, "publish_story"):
            try:
                cid = instagram.create_container(
                    cfg["access_token"], cfg["ig_user_id"],
                    image_url=media_url(cfg, date_str, "story"),
                    media_type="STORIES",
                )
                instagram.wait_finished(cfg["access_token"], cid)
                mid = instagram.publish(cfg["access_token"], cfg["ig_user_id"], cid)
                state.record_media_id(st, date_str, "story", mid)
                state.mark(st, date_str, "publish_story")
                state.save(st)
                log(f"story published: {mid}")
            except instagram.InstagramError as e:
                log(f"story publish FAILED: {e}")
                state.note_failure(st, date_str, "story", e)
                state.save(st)

    # Only mark the queue file as consumed when the feed step succeeded —
    # a half-finished day keeps its file so the re-run completes it.
    if state.done(st, date_str, "publish_feed"):
        state.mark_file_posted(st, filename)
        state.save(st)

    return 0 if state.both_published(st, date_str) else 1


# --- Stock-photo flow (Pexels, licensed) --------------------------------------

def run_stock_day(cfg: dict, date: dt.date, date_str: str, st: dict,
                  out_dir: pathlib.Path) -> int:
    """Publish one licensed stock photo (feed + Story). Returns 0 success,
    -1 'unavailable, fall back to quotes', 1 'started but incomplete'."""
    api_key = cfg.get("pexels_api_key", "")
    if not api_key:
        log("stock tier disabled (no PEXELS_API_KEY) — skipping")
        return -1

    # Idempotent re-run: look up the already-pinned photo for this date.
    pinned = state.stock_of_day(st, date_str)
    if pinned:
        fresh = pexels.photo_by_id(api_key, pinned["id"])
        if not fresh:
            log(f"ERROR: pinned stock photo #{pinned['id']} not fetchable — "
                "falling back to quotes")
            return -1
        photo = {**fresh, "vibe": pinned["vibe"]}
        log(f"resuming stock day with photo #{photo['id']} (idempotent re-run)")
    else:
        photo = pexels.pick_photo(api_key, date, state.used_stock_ids(st))
        if not photo:
            log("no stock photo available today — falling back to quotes")
            return -1
        state.set_stock_of_day(st, date_str, photo)
        state.save(st)
        log(f"stock photo #{photo['id']} ({photo['vibe']}) by "
            f"{photo['photographer']}")

    src_path = out_dir / f"{date_str}-stock.bin"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not pexels.download(photo, src_path):
        log("ERROR: could not download stock photo — falling back to quotes")
        return -1

    # Same photo treatment as the user's uploads: padded feed + Story.
    feed_path = out_dir / f"{date_str}-feed.jpg"
    story_path = out_dir / f"{date_str}-story.jpg"
    render.render_upload_feed(src_path, cfg["palette"], feed_path)
    render.render_upload_story(src_path, cfg["palette"], story_path)

    ok_feed, ok_story = push_media(cfg, date_str)
    if not (ok_feed and ok_story):
        log("ERROR: stock media not downloadable — cannot publish.")
        state.note_failure(st, date_str, "media_push",
                           "stock media not downloadable after push")
        state.save(st)
        return 1
    log("stock media padded and verified downloadable from media branch")

    bank = captions_mod.load_bank()
    caption = captions_mod.caption_text(bank, photo["vibe"], date,
                                        cfg["hashtags"])
    # Pexels attribution: required-style credit for a licensed stock photo.
    caption += f"\n\n📷 {photo['photographer']} on Pexels"

    _publish_image_pair_caption(cfg, st, date_str, caption)
    _note_token(cfg, st, date_str)

    if state.both_published(st, date_str):
        log(f"{date_str}: complete (stock photo: feed + story published).")
        return 0
    return 1


# --- Entry point ---------------------------------------------------------------

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
    date = (dt.date.fromisoformat(args.date or os.environ.get("POST_DATE_OVERRIDE")
                                  or "") if (args.date or os.environ.get("POST_DATE_OVERRIDE"))
            else dt.datetime.now(IST).date())
    date_str = date.isoformat()

    # --- Dry-run: render only ------------------------------------------------
    if args.dry_run:
        out_dir = PREVIEW_DIR
        result = build_media(cfg, date, out_dir)
        picked = result["picked"]
        log(f"dry-run ok — feed: {result['feed']}")
        log(f"dry-run ok — story: {result['story']}")
        log(f"quote: {picked['quote']['text'][:60]}…")
        log(f"tip: {picked['tip']['title']}")
        return 0

    # --- Full run: every exit path syncs alerts ---------------------------------
    # sync() never raises, so it can never mask the real exit code, and the
    # finally also covers crash paths that bypass every explicit return.
    # --prepare is a manual testing mode: no alert side effects.
    token_configured = bool(cfg["access_token"])
    try:
        return _run_publish(cfg, args, date, date_str)
    finally:
        alerts.sync(cfg, date_str, enabled=not args.prepare,
                    token_configured=token_configured)


def _run_publish(cfg: dict, args, date: dt.date, date_str: str) -> int:
    """The publishing run: secrets check → token housekeeping → content tiers."""
    if not cfg["access_token"]:
        log("ERROR: IG_ACCESS_TOKEN secret is not set — skipping run.")
        return 0  # exit 0 so a not-yet-configured repo doesn't show failures
    if not cfg["repo_owner"] or not cfg["repo_name"]:
        log("ERROR: repo_owner/repo_name missing from config.json — cannot "
            "build public media URLs.")
        return 1

    ig_user_id = cfg["ig_user_id"] or instagram.get_user_id(cfg["access_token"])
    cfg["ig_user_id"] = ig_user_id  # run_upload_day reads it from cfg

    st = state.load()

    # --- Token housekeeping (never blocks publishing) -------------------------
    # Runs BEFORE the "already published" early-return below so the token gets
    # its once-a-day refresh even on days whose post is already complete.
    # Meta allows one refresh per ~24h, so once a day succeeded we skip.
    if state.token_refreshed_today(st, date_str):
        log("token already refreshed today — skipping housekeeping")
    else:
        tok_out = token.refresh_and_store(
            cfg["access_token"], cfg["gh_pat"],
            f"{cfg['repo_owner']}/{cfg['repo_name']}",
        )
        cfg["access_token"] = tok_out["token"]
        if tok_out["refreshed"] and tok_out["secret_updated"]:
            log("token refreshed and IG_ACCESS_TOKEN secret updated")
            state.mark_token_refreshed(st, date_str)
        else:
            # Today still runs (possibly on a fresh in-memory token), but the
            # aging secret is a future-death risk — record it for alerting.
            log(f"WARNING: {tok_out['reason']}")
            state.note_token_refresh(st, date_str, tok_out["reason"])
        state.save(st)

    # Idempotency: nothing left to do means the run is complete.
    if state.both_published(st, date_str):
        log(f"{date_str}: feed + story already published — nothing to do.")
        return 0

    out_dir = ROOT / "media_out"

    # --- Content tiers: uploads queue -> stock photos -> quote/tip --------------
    try:
        rc = run_upload_day(cfg, date, date_str, st, out_dir)
    except Exception as e:  # never let a queue bug kill the daily post
        log(f"upload flow crashed ({e}) — falling back to stock/quotes")
        rc = 1
    if rc == 0:
        log(f"{date_str}: complete (uploads queue: feed + story published).")
        _note_token(cfg, st, date_str)
        return 0
    if rc == -1:
        log("uploads queue empty — trying the stock-photo tier")
        try:
            stock_rc = run_stock_day(cfg, date, date_str, st, out_dir)
        except Exception as e:  # stock tier must never kill the daily post
            log(f"stock flow crashed ({e}) — falling back to quotes")
            stock_rc = -1
        if stock_rc == 0:
            _note_token(cfg, st, date_str)
            return 0
        if stock_rc == 1:
            _note_token(cfg, st, date_str)
            log(f"{date_str}: stock day incomplete — safety re-run will retry.")
            return 1
        log("falling back to today's quote/tip graphics")
        st = state.load()
        if state.both_published(st, date_str):
            log(f"{date_str}: already complete after fallback check.")
            return 0
    else:
        # Partial failure: the safety re-run retries this same file.
        _note_token(cfg, st, date_str)
        log(f"{date_str}: upload day incomplete — safety re-run will retry.")
        return 1

    # --- Quote/tip fallback (original render flow) -----------------------------
    result = build_media(cfg, date, out_dir)
    picked = result["picked"]
    log(f"content: quote#N tip '{picked['tip']['title']}'")

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

    _publish_image_pair(cfg, st, date_str, picked)

    _note_token(cfg, st, date_str)

    # --- Exit code reflects completeness ---------------------------------------
    st = state.load()
    if state.both_published(st, date_str):
        log(f"{date_str}: complete (feed + story published).")
        return 0
    log(f"{date_str}: incomplete — safety re-run will retry missing steps.")
    return 1


def _publish_image_pair(cfg: dict, st: dict, date_str: str,
                        picked: dict) -> None:
    """Publish feed + story containers from the rendered quote/tip JPGs."""
    if not state.done(st, date_str, "publish_feed"):
        try:
            cid = instagram.create_container(
                cfg["access_token"], cfg["ig_user_id"],
                media_url(cfg, date_str, "feed"),
                caption=content.caption_for(picked["quote"], cfg["hashtags"]),
            )
            instagram.wait_finished(cfg["access_token"], cid)
            mid = instagram.publish(cfg["access_token"], cfg["ig_user_id"], cid)
            state.record_media_id(st, date_str, "feed", mid)
            state.mark(st, date_str, "publish_feed")
            state.save(st)
            log(f"feed published: {mid}")
        except instagram.InstagramError as e:
            log(f"feed publish FAILED: {e}")
            state.note_failure(st, date_str, "feed", e)
            state.save(st)

    if not state.done(st, date_str, "publish_story"):
        try:
            cid = instagram.create_container(
                cfg["access_token"], cfg["ig_user_id"],
                media_url(cfg, date_str, "story"), media_type="STORIES",
            )
            instagram.wait_finished(cfg["access_token"], cid)
            mid = instagram.publish(cfg["access_token"], cfg["ig_user_id"], cid)
            state.record_media_id(st, date_str, "story", mid)
            state.mark(st, date_str, "publish_story")
            state.save(st)
            log(f"story published: {mid}")
        except instagram.InstagramError as e:
            log(f"story publish FAILED: {e}")
            state.note_failure(st, date_str, "story", e)
            state.save(st)


def _publish_image_pair_caption(cfg: dict, st: dict, date_str: str,
                               caption: str) -> None:
    """Publish feed + story containers with a custom caption (stock flow)."""
    if not state.done(st, date_str, "publish_feed"):
        try:
            cid = instagram.create_container(
                cfg["access_token"], cfg["ig_user_id"],
                media_url(cfg, date_str, "feed"), caption=caption,
            )
            instagram.wait_finished(cfg["access_token"], cid)
            mid = instagram.publish(cfg["access_token"], cfg["ig_user_id"], cid)
            state.record_media_id(st, date_str, "feed", mid)
            state.mark(st, date_str, "publish_feed")
            state.save(st)
            log(f"feed published: {mid}")
        except instagram.InstagramError as e:
            log(f"feed publish FAILED: {e}")
            state.note_failure(st, date_str, "feed", e)
            state.save(st)

    if not state.done(st, date_str, "publish_story"):
        try:
            cid = instagram.create_container(
                cfg["access_token"], cfg["ig_user_id"],
                media_url(cfg, date_str, "story"), media_type="STORIES",
            )
            instagram.wait_finished(cfg["access_token"], cid)
            mid = instagram.publish(cfg["access_token"], cfg["ig_user_id"], cid)
            state.record_media_id(st, date_str, "story", mid)
            state.mark(st, date_str, "publish_story")
            state.save(st)
            log(f"story published: {mid}")
        except instagram.InstagramError as e:
            log(f"story publish FAILED: {e}")
            state.note_failure(st, date_str, "story", e)
            state.save(st)


def _note_token(cfg: dict, st: dict, date_str: str) -> None:
    """Token expiry bookkeeping (best-effort, never fatal)."""
    try:
        d_left, iso = token.days_left(cfg["access_token"])
        if d_left is not None:
            state.note_token_expiry(st, iso, d_left)
            if d_left < 7:
                log(f"WARNING: token expires in {d_left}d ({iso}) — refresh "
                    "the IG_ACCESS_TOKEN secret manually if it wasn't auto-updated.")
            state.save(st)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(run())
