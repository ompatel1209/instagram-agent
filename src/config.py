"""Configuration loading: config.json merged with environment secrets."""
import datetime as dt
import json
import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
CONTENT_DIR = ROOT / "content"
FONTS_DIR = ROOT / "assets" / "fonts"
PREVIEW_DIR = ROOT / "preview"

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Tokens ride in Graph GET query strings; a requests exception embeds that
# URL, and state.json is committed to a public repo. Scrub before storing.
_TOKEN_PARAM = re.compile(r"(access_token=)[^&\s'\"]+", re.IGNORECASE)
# The push scripts (scripts/push_file.sh, push_media.sh, ingest_uploads.sh)
# embed MEDIA_PUSH_TOKEN in remote URLs as x-access-token:<PAT>@github.com —
# a git/tool error echoing such a URL must not reach state.json either.
_URL_CRED = re.compile(r"(x-access-token:)[^@\s'\"]+(@)", re.IGNORECASE)
# Raw PAT / fine-grained PAT bodies in any other context.
_RAW_PAT = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")


def redact(text: object) -> str:
    """Strip access tokens from error text bound for state.json or a log."""
    out = _TOKEN_PARAM.sub(r"\1[redacted]", str(text))
    out = _URL_CRED.sub(r"\1[redacted]\2", out)
    return _RAW_PAT.sub("[redacted]", out)


def ist_today() -> dt.date:
    """Today in IST — the account's own calendar. Runners' local dates are
    UTC, which lags IST between 18:30–24:00 UTC (00:00–05:30 IST); day-keyed
    bookkeeping outside main/refresh's publishing flow uses this instead of
    dt.date.today() so per-day guards align with the IST day."""
    return dt.datetime.now(IST).date()


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    # Secrets come from the environment (GitHub Actions / local shell),
    # never from config.json — that file is committed to a public repo.
    cfg["access_token"] = os.environ.get("IG_ACCESS_TOKEN", "")
    cfg["ig_user_id"] = os.environ.get("IG_USER_ID", "")
    cfg["gh_pat"] = os.environ.get("GH_PAT", "")
    # Stock-photo tier (Pexels). Empty key = tier silently disabled.
    cfg["pexels_api_key"] = os.environ.get("PEXELS_API_KEY", "")
    # AI reply voice (NVIDIA NIM). Empty key = AI tier silently disabled,
    # engagement falls back to the replies.json bank.
    cfg["nvidia_api_key"] = os.environ.get("NVIDIA_API_KEY", "")

    # GitHub repo coordinates, used to build public media URLs and push paths.
    cfg.setdefault("repo_owner", os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
    cfg.setdefault("repo_name", os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1] or "")
    return cfg


def media_url(cfg: dict, date_str: str, kind: str) -> str:
    """Public raw.githubusercontent URL for a rendered media file."""
    return (
        f"https://raw.githubusercontent.com/{cfg['repo_owner']}/{cfg['repo_name']}"
        f"/media/{date_str}-{kind}.jpg"
    )


def reel_url(cfg: dict, date_str: str) -> str:
    """Public raw.githubusercontent URL for the day's Reel video file."""
    return (
        f"https://raw.githubusercontent.com/{cfg['repo_owner']}/{cfg['repo_name']}"
        f"/media/{date_str}-reel.mp4"
    )


def music_url(cfg: dict, filename: str) -> str:
    """Public raw.githubusercontent URL for a music-library track.

    `filename` carries its own music/ prefix (e.g. "music/rain-window.m4a"),
    exactly as stored in content/music.json and on the media branch.
    """
    return (
        f"https://raw.githubusercontent.com/{cfg['repo_owner']}/{cfg['repo_name']}"
        f"/media/{filename}"
    )
