"""Girly caption bank: pick a caption + hashtags by the vibe in a filename.

Files in the uploads queue are named like `selfie1.jpg` or `attitude2.mp4` —
the word before the trailing number is the vibe. Anything that doesn't match
a known vibe falls back to `general`, so no file is ever left captionless.
"""
import datetime as dt
import json
import random

from .config import CONTENT_DIR

# Filename word -> vibe key in content/captions.json (plus common synonyms).
VIBE_ALIASES = {
    "selfie": "selfie",
    "selfy": "selfie",
    "pic": "selfie",
    "photo": "selfie",
    "attitude": "attitude",
    "att": "attitude",
    "sassy": "attitude",
    "cute": "cute",
    "cutie": "cute",
    "ootd": "ootd",
    "outfit": "ootd",
    "dress": "ootd",
    "fashion": "ootd",
    "travel": "travel",
    "trip": "travel",
    "vacation": "travel",
    "vacay": "travel",
    "beach": "travel",
    "selflove": "selflove",
    "love": "selflove",
    "glow": "selflove",
    "general": "general",
}

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}


def load_bank() -> dict:
    with open(CONTENT_DIR / "captions.json", encoding="utf-8") as f:
        return json.load(f)


def vibe_from_filename(filename: str) -> str:
    """`attitude3.mp4` -> `attitude`; unknown -> `general`."""
    stem = filename.rsplit(".", 1)[0].lower().strip()
    stem = "".join(ch for ch in stem if ch.isalpha())  # drop digits
    return VIBE_ALIASES.get(stem, "general")


def is_video(filename: str) -> bool:
    return "." + filename.rsplit(".", 1)[-1].lower() in VIDEO_EXTS


def pick(bank: dict, vibe: str, date: dt.date) -> dict:
    """Deterministically pick the caption for a date (stable on re-runs)."""
    entries = bank.get(vibe) or bank["general"]
    rng = random.Random(f"{date.isoformat()}:{vibe}")
    captions = entries["captions"]
    n = dt.date.fromisoformat(date.isoformat()).toordinal() - dt.date(2026, 9, 2).toordinal()
    caption = captions[n % len(captions)]
    return {"caption": caption, "hashtags": entries["hashtags"], "rng": rng}


def caption_text(bank: dict, vibe: str, date: dt.date, extra_tags: list[str]) -> str:
    """Full caption body: girly line + vibe hashtags (+ optional extras)."""
    picked = pick(bank, vibe, date)
    tags = list(picked["hashtags"]) + [t for t in extra_tags if t]
    lines = [picked["caption"]]
    if tags:
        lines += ["", " ".join(f"#{t.lstrip('#').lower()}" for t in tags)]
    return "\n".join(lines)
