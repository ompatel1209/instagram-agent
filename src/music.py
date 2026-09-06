"""Music library for Reels: 18 licensed tracks on the media branch.

The Graph API can't attach licensed IG-library music to a media container,
so the copyright-safe path (proven live on Reel 18104882834264326) is to
pre-embed audio in the video file itself. This module is the reusable half
of that: it knows the library manifest (content/music.json), picks a track
deterministically per date, downloads it from raw.githubusercontent.com,
and muxes it under a silent video with the proven ffmpeg command.

The .m4a files live on the `media` branch under music/ (one bulk push,
2026-09-06); the manifest maps each file to an id, mood, and duration.
Track audio originates from validated Pexels videos (same ids as the old
AUDIO_SOURCES rotation in reel.py) — licensed, safe to mux and publish.

Every failure mode is non-fatal: callers fall back to publishing silent
(or the bank caption) and record a failure note in state, so a broken
library never blocks a day's post.
"""
import datetime as dt
import json
import pathlib
import subprocess

import requests

from .config import CONTENT_DIR, music_url

MANIFEST_PATH = CONTENT_DIR / "music.json"
EPOCH = dt.date(2026, 9, 2)

_library_cache = None


def library() -> list:
    """Load the track manifest (cached). Empty list on any failure."""
    global _library_cache
    if _library_cache is None:
        try:
            with open(MANIFEST_PATH, encoding="utf-8") as f:
                _library_cache = json.load(f).get("tracks", [])
        except (OSError, ValueError):
            _library_cache = []
    return _library_cache


def pick_track(date: dt.date) -> dict | None:
    """Deterministic per-date rotation through the library.

    Same (date, library) always picks the same track, so idempotent
    re-runs never mux a different song over an already-published video.
    """
    tracks = library()
    if not tracks:
        return None
    n = date.toordinal() - EPOCH.toordinal()
    return tracks[n % len(tracks)]


def download_track(cfg: dict, track: dict, dest) -> bool:
    """Fetch one library track to the runner. False on any failure."""
    url = music_url(cfg, track["filename"])
    try:
        r = requests.get(url, timeout=120)
        if r.status_code == 200:
            pathlib.Path(dest).write_bytes(r.content)
            return True
    except requests.RequestException:
        pass
    return False


def has_audio(path) -> bool:
    """True when the video file carries an audio stream (ffprobe)."""
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


def mux(video_path, audio_path, out_path) -> bool:
    """Loop the short audio track under the video, trim to the video,
    re-encode audio to AAC, copy the video stream untouched (fast).

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
        return p.returncode == 0 and pathlib.Path(out_path).exists()
    except (subprocess.TimeoutExpired, OSError):
        return False
