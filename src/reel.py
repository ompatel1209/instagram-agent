"""Daily Reel tier: stock Pexels video + embedded Pexels audio (non-fatal).

The Instagram Graph API cannot attach licensed IG-library music to a media
container, so the copyright-safe path for "aesthetic music on every Reel"
is to pre-embed audio in the video file itself. This module builds that
file: a portrait Pexels stock video (visual mood matching the day's vibe)
muxed with audio from another licensed Pexels video, then pushed to the
media branch and published as a Reel.

Priority in src/main.py: the day's uploads-queue file IS the reel when it
is a video (run_upload_day marks publish_reel); this module only fills the
reel slot on days with no queued video.

Every failure mode is non-fatal: the caller records a "reel" failure in
state and the day exits non-zero so the safety re-run retries — but feed
and story never depend on this module, so their guarantee is untouched.

AUDIO_SOURCES are Pexels video ids whose mp4s carry embedded audio,
validated by probe (ffprobe audio codec present). The list rotates by day
so the same track doesn't back every reel; extend it with more validated
ids as they're found.
"""
import datetime as dt
import subprocess
import time

import requests

from . import captions as captions_mod
from . import instagram, pexels, state
from .config import reel_url

# Validated audio-bearing Pexels videos (id, mood tag, seconds of audio).
# All are licensed Pexels content — safe to mux and publish.
AUDIO_SOURCES = [
    10411103,  # ambient guitar — the original live-tested track
    7102561,   # indian dance instrumental
    7102478,   # indian dance instrumental
    8872661,   # flowers / nature
    11341089,  # flowers / nature
    7249122,   # henna close-up ambience
    10340016,  # henna close-up ambience
    12193285,  # sunset nature
    9246884,   # coffee aesthetic
    9939786,   # coffee aesthetic
    12742709,  # rain window
    17713400,  # rain window
    19997487,  # candle flame
    15667292,  # candle flame
    6227239,   # candle flame
    6270175,   # candle flame
    6973160,   # aesthetic lofi
    20165036,  # chill nature
]
EPOCH = dt.date(2026, 9, 2)


def _ffprobe_has_audio(path) -> bool:
    """True when the video file carries an audio stream."""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0",
             str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return bool(p.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return False


def _download_audio_source(cfg: dict, date: dt.date, dest) -> bool:
    """Fetch the day's rotating audio source; False on any failure."""
    n = date.toordinal() - EPOCH.toordinal()
    source_id = AUDIO_SOURCES[n % len(AUDIO_SOURCES)]
    audio_video = pexels.video_by_id(cfg["pexels_api_key"], source_id)
    if not audio_video:
        return False
    return pexels.download_video_file(audio_video, dest)


def _mux_music(video_path, audio_path, out_path) -> bool:
    """Loop the short audio track under the video, trim to the video,
    re-encode audio to AAC, keep video stream untouched (fast).

    Proven live: published as Reel 18104882834264326 on @whoisaaniiiya.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-stream_loop", "-1", "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return p.returncode == 0 and out_path.exists()
    except (subprocess.TimeoutExpired, OSError):
        return False


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
        print("reel: PEXELS_API_KEY unset — reel tier disabled")
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
    if _ffprobe_has_audio(src_path):
        print("reel: source video already carries audio — no mux needed")
        src_path.rename(final_path)
    else:
        audio_path = out_dir / f"{date_str}-audio.mp4"
        if not _download_audio_source(cfg, date, audio_path):
            print("reel: audio source download failed — publishing silent")
            src_path.rename(final_path)
        elif not _mux_music(src_path, audio_path, final_path):
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
