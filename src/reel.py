"""Daily Reel tier: stock Pexels video + embedded library music (non-fatal).

The Instagram Graph API cannot attach licensed IG-library music to a media
container, so the copyright-safe path for "aesthetic music on every Reel"
is to pre-embed audio in the video file itself. This module builds that
file: a portrait stock video (visual mood matching the day's vibe) muxed
with a track from the local music library (src/music.py), then pushed to
the media branch and published as a Reel.

Priority in src/main.py: the day's uploads-queue file IS the reel when it
is a video (run_upload_day muxes library music into silent queue files
itself); this module only fills the reel slot on days with no queued
video.

The stock-VIDEO half needs the Pexels API (key removed 2026-09-05, so
this gap-fill tier is dormant until a key returns); the AUDIO half uses
only the local library on the media branch and needs no key at all.

Every failure mode is non-fatal: the caller records a "reel" failure in
state and the day exits non-zero so the safety re-run retries — but feed
and story never depend on this module, so their guarantee is untouched.
"""
import datetime as dt
import subprocess
import time

import requests

from . import captions as captions_mod
from . import instagram, music, pexels, state
from .config import reel_url


def _wait_fetchable(url: str, tries: int = 20, delay: int = 15) -> bool:
    """Poll raw.githubusercontent until the pushed file is downloadable.

    Mirrors main.push_media's fetchability gate: Meta must be able to
    fetch the video URL at container creation. Video files are larger, so
    the poll is patient (20 × 15s ≈ 5 minutes max).
    """
    for _ in range(tries):
        try:
            r = requests.head(url, timeout=30)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(delay)
    return False


def _caption(cfg: dict, date: dt.date, video: dict) -> str:
    """Girly caption matching the day's vibe + Pexels credits."""
    vibe = video.get("vibe") or "general"
    bank = captions_mod.load_bank()
    caption = captions_mod.caption_text(bank, vibe, date, cfg["hashtags"])
    return caption + (
        f"\n\n🎥 {video.get('photographer', 'Pexels')} on Pexels"
        f"\n🎵 Music: Pexels"
    )


def run(cfg: dict, date: dt.date, date_str: str, st: dict, out_dir) -> bool:
    """Fill the day's reel slot with a stock video + music. True on success.

    Non-fatal by contract: any failure returns False — the caller records
    a "reel" failure and exits non-zero so the safety re-run retries.
    """
    if not cfg.get("pexels_api_key"):
        print("reel: PEXELS_API_KEY unset — stock-video reel tier disabled")
        return False

    out_dir.mkdir(parents=True, exist_ok=True)

    # Resume with the pinned pick when a re-run needs to redo a step.
    pinned = state.reel_of_day(st, date_str)
    if pinned:
        video = pexels.video_by_id(cfg["pexels_api_key"], pinned["id"])
        if not video:
            print(f"reel: pinned video {pinned['id']} lookup failed")
            return False
        video["vibe"] = pinned.get("vibe")
    else:
        video = pexels.pick_video(cfg["pexels_api_key"], date,
                                  state.used_reel_ids(st))
        if not video:
            print("reel: no usable stock video for today's vibe")
            return False
        state.set_reel_of_day(st, date_str, video)

    src_path = out_dir / f"{date_str}-reel-src.mp4"
    if not pexels.download_video_file(video, src_path):
        print(f"reel: video {video['id']} download failed")
        return False

    final_path = out_dir / f"{date_str}-reel.mp4"
    if music.has_audio(src_path):
        print("reel: source video already carries audio — no mux needed")
        src_path.rename(final_path)
    else:
        track = music.pick_track(date)
        audio_path = out_dir / f"{date_str}-audio.m4a"
        if not track or not music.download_track(cfg, track, audio_path):
            print("reel: library track download failed — publishing silent")
            src_path.rename(final_path)
            state.note_failure(st, date_str, "reel",
                               "library track download failed — published silent")
        elif not music.mux(src_path, audio_path, final_path):
            # Silent beats missing: still publish, still note the failure.
            print("reel: ffmpeg mux failed — publishing without music")
            src_path.rename(final_path)
            state.note_failure(st, date_str, "reel",
                               "music mux failed — published without music")

    push = subprocess.run(
        ["bash", "scripts/push_file.sh", str(final_path),
         f"{date_str}-reel.mp4"],
        capture_output=True, text=True, timeout=600,
    )
    if push.returncode != 0:
        print(f"reel: push failed — {push.stderr.strip()[:200]}")
        return False

    url = reel_url(cfg, date_str)
    if not _wait_fetchable(url):
        print("reel: pushed file never became fetchable")
        return False

    if state.done(st, date_str, "publish_reel"):
        print("reel: already published — file refreshed only")
        return True

    try:
        cid = instagram.create_container(
            cfg["access_token"], cfg["ig_user_id"],
            video_url=url, media_type="REELS", caption=_caption(cfg, date, video),
        )
        instagram.wait_finished(cfg["access_token"], cid,
                                max_wait_s=600, poll_s=30)
        mid = instagram.publish(cfg["access_token"], cfg["ig_user_id"], cid)
    except instagram.InstagramError as e:
        print(f"reel: publish failed — {e}")
        return False

    state.record_media_id(st, date_str, "reel", mid)
    state.mark(st, date_str, "publish_reel")
    print(f"reel: published {mid}")
    return True
